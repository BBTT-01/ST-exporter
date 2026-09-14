"""Download pricebook images on the runner, push the bytes to TrueQuote.

One pass over the pricebook records the feed has *already fetched* — this never
re-lists the catalogue. For each item it picks the single asset TrueQuote's own
selection rule would pick (``assets.select_uploadable_asset``), resolves the two
identifier forms the Sheet's ``image_refs`` column can name, and POSTs the bytes.

Resolving the two forms (both documented on
``st_exporter.pricebook.asset_identifier``):

- ``https://…`` — fetched directly, unauthenticated, exactly as TrueQuote's
  direct mode would have rendered it in the browser.
- ``Images/Pricebook/<uuid>.jpg`` — fetched from ServiceTitan's authenticated
  ``pricebook/v2/tenant/{id}/images?path=…``, the same endpoint and the same
  ``path`` parameter TrueQuote's own ``downloadPricebookImage`` uses
  (``packages/servicetitan/src/server.ts:437``). This is the call that needs the
  separate ``Pricebook → Images`` permission.

Failure policy, in one place because it is the whole point of the module:

- A 403 from ServiceTitan's images endpoint is ``permission_denied`` — a NAMED,
  non-fatal outcome. The contractor may simply not have ticked
  ``Pricebook → Images``. The first one stops further authenticated downloads
  (the permission is tenant-wide, so the 404th 403 teaches us nothing the first
  did not) and the run reports it; public HTTPS assets keep uploading.
- Any single asset failing — download or upload — is counted and stepped over.
  A pricebook run must not end because one image is a broken link.
- 401/429/5xx from TrueQuote — **and a TrueQuote request that never reached a
  status at all**, a DNS failure or a timeout — ends the *pass*, not the run:
  those are about the connection, and every unsent asset is simply retried by
  the next scheduled run. Nothing is lost because nothing here is a queue — the
  catalogue is the queue. ``images/client.py`` converts the transport failure
  into a retryable rejection so this stays a promise about *outcomes*, not one
  about HTTP status codes that an exception could walk straight past.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from st_cli.client import ServiceTitanClient
from st_cli.exceptions import APIError, STCLIError
from st_exporter.images.assets import (
    MAX_IMAGE_BYTES,
    PricebookAsset,
    idempotency_key,
    is_storage_path,
    select_uploadable_asset,
    sniff_content_type,
)
from st_exporter.images.client import (
    ImageUploadAccepted,
    ImageUploadRejected,
    TrueQuoteImageClient,
)
from st_exporter.images.ledger import ImageLedger, ImageLedgerEntry
from st_exporter.logging_setup import logger

_IMAGES_MODULE = "pricebook"
_IMAGES_RESOURCE = "images"
_PUBLIC_TIMEOUT = 60.0

# TrueQuote rejects the whole upload when a metadata field exceeds this
# (route.ts:14,36). Dropping an over-long alias keeps the image; sending it
# loses the image to a 422.
_MAX_TEXT_FIELD = 300


@dataclass
class ImageUploadSummary:
    """What one image pass did. Every counter is a distinct, named outcome."""

    considered: int = 0
    no_image: int = 0
    already_uploaded: int = 0
    uploaded: int = 0
    download_failed: int = 0
    upload_rejected: int = 0
    unsupported: int = 0
    too_large: int = 0
    # True when ServiceTitan answered 403 on the authenticated images endpoint:
    # the tenant has not granted `Pricebook → Images`. Not an error — a fact the
    # run has to state out loud.
    permission_denied: bool = False
    # Set when the pass stopped early (TrueQuote unreachable/rate limited). The
    # remaining assets are untouched and the next run retries them.
    stopped: str | None = None
    seen_keys: set[str] = field(default_factory=set)

    @property
    def complete(self) -> bool:
        """True when the pass saw every asset it set out to see, and saw it WHOLE.

        ``download_failed`` counts too. A CDN 500 on one image means that
        image's key never reached ``seen_keys``, so pruning the ledger against
        ``seen_keys`` would forget an upload that genuinely happened and re-send
        identical bytes on the next run. "Could not fetch it" is not "it is
        gone" — the same distinction ``permission_denied`` already makes, one
        asset at a time instead of tenant-wide.
        """
        return self.stopped is None and not self.permission_denied and self.download_failed == 0

    def as_log_fields(self) -> str:
        return (
            f"considered={self.considered} uploaded={self.uploaded} "
            f"already={self.already_uploaded} no_image={self.no_image} "
            f"download_failed={self.download_failed} rejected={self.upload_rejected} "
            f"unsupported={self.unsupported} too_large={self.too_large} "
            f"permission_denied={str(self.permission_denied).lower()} "
            f"stopped={self.stopped or 'no'}"
        )


def upload_pricebook_images(
    client: ServiceTitanClient,
    image_client: TrueQuoteImageClient,
    ledger: ImageLedger,
    records: Iterable[dict[str, Any]],
    *,
    now: str,
    http: httpx.Client | None = None,
) -> ImageUploadSummary:
    """Upload one image per pricebook item. Never raises for a single bad asset."""
    summary = ImageUploadSummary()
    owns_http = http is None
    public = http or httpx.Client(timeout=_PUBLIC_TIMEOUT, follow_redirects=True)

    try:
        for record in records:
            asset = select_uploadable_asset(record)
            if asset is None:
                summary.no_image += 1
                continue
            summary.considered += 1
            if not _upload_one(client, image_client, ledger, public, asset, summary, now=now):
                break
    finally:
        if owns_http:
            public.close()

    return summary


def _upload_one(
    client: ServiceTitanClient,
    image_client: TrueQuoteImageClient,
    ledger: ImageLedger,
    public: httpx.Client,
    asset: PricebookAsset,
    summary: ImageUploadSummary,
    *,
    now: str,
) -> bool:
    """Handle one asset. Returns False when the whole pass must stop."""
    authenticated = is_storage_path(asset.source_url)
    if authenticated and summary.permission_denied:
        # Already told, once, that this tenant will not serve image bytes.
        return True

    payload = _download(client, public, asset, summary)
    if payload is None:
        return True

    if len(payload) > MAX_IMAGE_BYTES:
        summary.too_large += 1
        logger.info("pricebook image over the %d byte cap; skipped", MAX_IMAGE_BYTES)
        return True

    content_type = sniff_content_type(payload)
    if content_type is None:
        # TrueQuote would 422 these (`image_content_mismatch`); no point sending.
        summary.unsupported += 1
        return True

    key = idempotency_key(asset, payload)
    summary.seen_keys.add(key)
    if ledger.has(key):
        summary.already_uploaded += 1
        return True

    if len(asset.identity) > _MAX_TEXT_FIELD:
        summary.unsupported += 1
        return True

    result = image_client.upload(
        external_item_id=asset.external_item_id,
        source_url=asset.source_url,
        content_type=content_type,
        payload=payload,
        idempotency_key=key,
        asset_id=asset.asset_id,
        filename=_capped(asset.filename),
        alias=_capped(asset.alias),
        asset_type=_capped(asset.asset_type),
        is_default=asset.is_default,
    )

    if isinstance(result, ImageUploadAccepted):
        summary.uploaded += 1
        ledger.record(
            ImageLedgerEntry(
                idempotency_key=key,
                asset_ref=f"{asset.external_item_id}:{asset.identity}",
                storage_path=result.storage_path,
                uploaded_at=now,
            )
        )
        return True

    assert isinstance(result, ImageUploadRejected)
    if result.retryable:
        summary.stopped = result.error
        logger.warning(
            "TrueQuote image upload stopped after HTTP %d (%s); "
            "the remaining images are retried next run",
            result.status_code,
            result.error,
        )
        return False

    summary.upload_rejected += 1
    logger.warning(
        "TrueQuote refused one pricebook image (HTTP %d %s); the run continues",
        result.status_code,
        result.error,
    )
    return True


def _download(
    client: ServiceTitanClient,
    public: httpx.Client,
    asset: PricebookAsset,
    summary: ImageUploadSummary,
) -> bytes | None:
    """Bytes for one asset, or None when it could not be fetched (already counted)."""
    try:
        if is_storage_path(asset.source_url):
            payload, _ = client.get_bytes(
                _IMAGES_MODULE, _IMAGES_RESOURCE, params={"path": asset.source_url}
            )
            return payload
        resp = public.get(asset.source_url)
        resp.raise_for_status()
        return resp.content
    except APIError as exc:
        if exc.status_code == 403:
            summary.permission_denied = True
            logger.warning(
                "ServiceTitan refused the pricebook images endpoint (403). The tenant has "
                "not granted `Pricebook -> Images`; pricebook image upload is skipped this "
                "run. Every other tab is unaffected."
            )
        else:
            summary.download_failed += 1
            logger.info("pricebook image download failed: %s", exc)
        return None
    except (STCLIError, httpx.HTTPError) as exc:
        summary.download_failed += 1
        logger.info("pricebook image download failed: %s", exc)
        return None


def _capped(value: str | None) -> str | None:
    """Drop an over-long optional metadata field rather than lose the image to a 422."""
    if value is None or len(value) > _MAX_TEXT_FIELD:
        return None
    return value

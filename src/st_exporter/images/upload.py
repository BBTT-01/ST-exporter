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
  A pricebook run must not end because one image is a broken link. The one
  exception is a ``TransportError`` from ServiceTitan's authenticated images
  endpoint: that is the connection, not the asset, so it stops the pass the way
  a 403 does rather than burning a retry budget per asset until the workflow's
  ``timeout-minutes`` kills the run before `_meta` is written.
- 401/429/5xx from TrueQuote — **and a TrueQuote request that never reached a
  status at all**, a DNS failure or a timeout — ends the *pass*, not the run:
  those are about the connection, and every unsent asset is simply retried by
  the next scheduled run. Nothing is lost because nothing here is a queue — the
  catalogue is the queue. ``images/client.py`` converts the transport failure
  into a retryable rejection so this stays a promise about *outcomes*, not one
  about HTTP status codes that an exception could walk straight past.

The pass is BOUNDED, ORDERED and CHEAP TO RESUME, and the three only work
together. Run 35130164187 on `BBTT-01/tr-doorservpro` is why all three exist: a
~7,191-asset catalogue, SIGKILLed at 10m35s, 0 images uploaded — every hour,
for ever, with no way for it to ever converge.

- ``deadline`` stops the pass cleanly while the job is still alive. The runner's
  ``timeout-minutes`` does not stop a pass, it SIGKILLs the process, and a killed
  process never flushes the ledger: every upload that run made is forgotten and
  re-sent by the next one.
- The ORDER is oldest-verification-first (``ImageLedger.last_verified``), so a
  bounded pass resumes where the last one stopped instead of re-treading the
  same prefix. An asset the ledger has never seen sorts before one already
  confirmed, so a first sweep reaches the whole catalogue in as few runs as the
  budget allows; afterwards the same order is a fair rotation.
- The ledger check happens BEFORE the download wherever it provably can
  (``_is_still_fresh``). The idempotency key hashes the PAYLOAD — deliberately,
  so changed bytes are re-sent — which by itself forces a download of every
  asset just to rediscover it was already delivered. That is the root
  inefficiency, and the item's ``modifiedOn`` is the cheap validator that
  dissolves it: an item ServiceTitan has not modified since the ledger last
  confirmed its asset cannot have new bytes, so the download is skipped outright.

THE DECISION ORDER, THREE STEPS, CHEAPEST FIRST
===============================================

1. ``modifiedOn`` unchanged since the ledger's last verification, and that
   verification is younger than ``REVERIFY_AFTER_DAYS`` — skip entirely. **No
   request at all.** Counted as ``revalidated``.
2. Otherwise fetch it, quoting back whatever ``ETag``/``Last-Modified`` the
   ledger stored for it (``images/conditional.py``). A **304** means the server
   checked and there are no new bytes: verification refreshed, nothing
   downloaded, nothing uploaded. Counted as ``not_modified``.
3. A **200** is new bytes and proceeds exactly as it always did — hash, dedupe,
   upload — and the response's validators are stored so step 2 can be cheap next
   time.

Step 2 is what replaced a blanket weekly RE-DOWNLOAD of the whole catalogue. The
weekly sweep still happens, at the same interval, for the same reason (the
``modifiedOn`` in step 1 is an ITEM-level timestamp standing in for an
ASSET-level change, and nobody has confirmed on a live tenant that replacing an
image moves it — KNOWN_UNVERIFIED.md). What changed is its price: a request and
a 304 instead of an image.

**Nothing here assumes ServiceTitan honours conditional requests.** With no
stored validator the fetch is a plain GET and the behaviour is identical to
before; with a validator that the server ignores, the answer is a 200 and the
behaviour is identical to before. The one thing the pass insists on is measuring
which of those is happening — ``ConditionalReport`` counts it and the pass logs
one line per run saying so in English.

Neither the order nor the ledger is a queue. The catalogue is still the queue,
and the ledger still only ever says what has already been delivered: losing it
costs bandwidth, never correctness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Iterable

import httpx

from st_cli.client import ServiceTitanClient
from st_cli.exceptions import APIError, STCLIError, TransportError
from st_exporter.images.assets import (
    MAX_IMAGE_BYTES,
    PricebookAsset,
    describe_rejected_payload,
    idempotency_key,
    is_storage_path,
    payload_shape,
    redact_source_url,
    select_uploadable_asset,
    sniff_content_type,
)
from st_exporter.images.client import (
    ImageUploadAccepted,
    ImageUploadRejected,
    TrueQuoteImageClient,
)
from st_exporter.images.conditional import ConditionalReport, Validators
from st_exporter.images.ledger import ImageLedger, ImageLedgerEntry
from st_exporter.logging_setup import logger

_IMAGES_MODULE = "pricebook"
_IMAGES_RESOURCE = "images"
_PUBLIC_TIMEOUT = 60.0

# TrueQuote rejects the whole upload when a metadata field exceeds this
# (route.ts:14,36). Dropping an over-long alias keeps the image; sending it
# loses the image to a 422.
_MAX_TEXT_FIELD = 300

# The one `stopped` reason a larger `job_timeout_minutes` fixes.
BUDGET_SPENT = "time budget for the image pass spent"

# The other `stopped` reason, and a DIFFERENT knob: `EXPORTER_IMAGE_MAX_ASSETS`.
# Deliberately distinguishable from BUDGET_SPENT in the run's own output,
# because the two ask for opposite fixes — one for more minutes, one for a
# larger (or no) cap — and a single "stopped" would send an operator to the
# wrong dial.
ASSET_CAP_REACHED = "per-run asset cap reached"

# No cap at all, and the DEFAULT. A number here would silently truncate a large
# catalogue for ever on any caller that never thought about it, and the failure
# mode would be invisible: a tenant permanently missing its last N images with a
# clean-looking run. Unlimited is the honest default; a caller that wants the
# pass bounded says so, and the run then says so back (see ASSET_CAP_REACHED).
NO_ASSET_CAP = 0

# How many REJECTED payloads get a full WARNING line describing what arrived.
#
# The detail exists to answer one question per run — "are these HTML error
# pages or are they GIFs" — and a handful of examples answers it as well as
# 16,112 would. Past the cap the pass says so once and keeps only the counts
# (`ImageUploadSummary.unsupported_shapes`), which are O(1) and always complete.
UNSUPPORTED_DETAIL_LIMIT = 5

# However fresh `modifiedOn` says an asset is, RE-VERIFY it after this long.
#
# `_is_still_fresh` rests on ServiceTitan bumping an item's `modifiedOn` when
# its image is replaced, which no live tenant has been used to confirm (see
# KNOWN_UNVERIFIED.md). If that assumption is ever wrong the cost must be a
# delay, not a permanently wrong image — so every asset is re-verified against
# the SERVER at least this often, whatever the timestamps say.
#
# What changed with conditional requests is the PRICE of that insurance, not the
# interval. It used to mean a full re-download of every asset every week — on
# ~7,191 assets, for ever. It now means a conditional GET: for any asset whose
# server returned an `ETag` or a `Last-Modified`, the answer is a 304 with no
# body, and the weekly sweep costs one request instead of one image. Only assets
# whose server offered NO validator still pay the full download here, which is
# precisely the old behaviour preserved for the case the new mechanism cannot
# cover. The interval stays at 7 because the staleness bound it buys is
# unchanged and it is now nearly free; lifting it would only trade away
# freshness for nothing.
REVERIFY_AFTER_DAYS = 7


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
    # Set when the pass stopped early (TrueQuote unreachable/rate limited, or
    # the time budget spent). The remaining assets are untouched and the next
    # run picks them up FIRST, because they are the least recently verified.
    stopped: str | None = None
    # Assets skipped without downloading, because the ledger had already
    # confirmed them and ServiceTitan says the item has not changed since. The
    # counter that makes a converged sweep legible: on a steady-state catalogue
    # almost everything lands here and the pass costs almost nothing.
    revalidated: int = 0
    # Assets the SERVER confirmed unchanged with a 304: a request was made, no
    # bytes came back, nothing was uploaded, and the verification timestamp was
    # refreshed. Distinct from `revalidated`, which cost no request at all.
    not_modified: int = 0
    # Assets this run actually fetched over the network, 304s included. This is
    # what `max_assets` caps — a `modifiedOn` skip is free and must not consume
    # a budget that exists to bound WORK.
    fetched: int = 0
    # The cap this run was given (`NO_ASSET_CAP` = uncapped). Carried on the
    # summary, not just read from config, so ONE log line answers both halves of
    # "why did it stop": the number that was in force, and whether it bit.
    max_assets: int = NO_ASSET_CAP
    # What the wire said about conditional-request support. The measurement half
    # of this feature: nobody has confirmed ServiceTitan honours `If-None-Match`,
    # so the pass counts rather than assumes. See `images/conditional.py`.
    conditional: ConditionalReport = field(default_factory=ConditionalReport)
    # Assets this pass never reached. Zero on a pass that saw the catalogue out.
    # The number an operator watches: it falls run over run while a first sweep
    # converges, and a run that leaves it high with `stopped=budget` is asking
    # for a larger `job_timeout_minutes`, not for a bug report.
    pending: int = 0
    # What the payloads counted in `unsupported` LOOKED like, by cheap prefix
    # check: `{"html": 5}` and `{"gif": 5}` are the same counter and opposite
    # diagnoses. Carried on the summary so one line answers the question even
    # when the per-asset detail lines have hit `UNSUPPORTED_DETAIL_LIMIT`.
    unsupported_shapes: dict[str, int] = field(default_factory=dict)
    # How many rejections have already been described in full. Bounds the log,
    # never the counting.
    unsupported_detail_logged: int = 0
    seen_keys: set[str] = field(default_factory=set)

    @property
    def unsupported_shapes_field(self) -> str:
        """``html:5,gif:1`` — commonest first — or ``none``."""
        if not self.unsupported_shapes:
            return "none"
        ordered = sorted(self.unsupported_shapes.items(), key=lambda kv: (-kv[1], kv[0]))
        return ",".join(f"{shape}:{count}" for shape, count in ordered)

    @property
    def cap_hit(self) -> bool:
        """True when this run stopped because of the asset cap, not the clock."""
        return self.stopped == ASSET_CAP_REACHED

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
            f"unsupported={self.unsupported} "
            f"unsupported_shapes={self.unsupported_shapes_field} "
            f"too_large={self.too_large} "
            f"revalidated={self.revalidated} not_modified={self.not_modified} "
            f"fetched={self.fetched} "
            f"max_assets={self.max_assets or 'none'} "
            f"cap_hit={str(self.cap_hit).lower()} pending={self.pending} "
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
    deadline: float | None = None,
    max_assets: int = NO_ASSET_CAP,
    http: httpx.Client | None = None,
) -> ImageUploadSummary:
    """Upload one image per pricebook item. Never raises for a single bad asset.

    ``deadline`` is a ``time.monotonic()`` reading past which no further asset is
    started. None means no budget, which is only safe where nothing will kill the
    process — in tests, and in a local run.

    ``max_assets`` caps how many assets this run FETCHES — how many reach the
    network at all. ``NO_ASSET_CAP`` (0, the default) means no cap. Two stopping
    conditions, not one, because they bound different things: the deadline bounds
    the CLOCK (and the thing it protects is the ledger flush), the cap bounds the
    WORK (and the thing it protects is a tenant's rate limit and bandwidth while
    a strategy settles). They set different ``stopped`` reasons and both flush,
    because both leave the pass at the end of a whole asset.

    A `modifiedOn` skip never consumes the cap. It costs no request, and a cap
    that counted it would stop a converged catalogue part-way through a sweep it
    could have finished for free — and, worse, would make every run report
    ``stopped`` and so never prune the ledger.
    """
    summary = ImageUploadSummary(max_assets=max_assets)
    owns_http = http is None
    public = http or httpx.Client(timeout=_PUBLIC_TIMEOUT, follow_redirects=True)

    assets = _ordered_assets(records, ledger, summary)
    attempted = 0
    try:
        for asset in assets:
            # Both checks happen BEFORE the asset is started, never in the
            # middle of one: the point is to end on a whole asset with a
            # flushable ledger.
            if deadline is not None and monotonic() >= deadline:
                summary.stopped = BUDGET_SPENT
                break
            if max_assets != NO_ASSET_CAP and summary.fetched >= max_assets:
                summary.stopped = ASSET_CAP_REACHED
                break
            attempted += 1
            summary.considered += 1
            if not _upload_one(client, image_client, ledger, public, asset, summary, now=now):
                break
    finally:
        if owns_http:
            public.close()

    summary.pending = len(assets) - attempted
    if summary.stopped == BUDGET_SPENT:
        logger.warning(
            "image pass stopped on its time budget with %d asset(s) still to visit; the "
            "next run starts with them, because they are the least recently verified. "
            "Raise the caller job's `job_timeout_minutes` to converge sooner.",
            summary.pending,
        )
    elif summary.stopped == ASSET_CAP_REACHED:
        logger.warning(
            "image pass stopped on its per-run asset cap (EXPORTER_IMAGE_MAX_ASSETS=%d) "
            "after fetching %d asset(s), with %d still to visit; the next run starts "
            "with them, because they are the least recently verified. Raise or remove "
            "the cap (0 = no cap) to converge sooner.",
            max_assets,
            summary.fetched,
            summary.pending,
        )
    logger.info(
        "image conditional requests: %s -- %s",
        summary.conditional.as_log_fields(),
        summary.conditional.verdict(),
    )
    return summary


def _ordered_assets(
    records: Iterable[dict[str, Any]],
    ledger: ImageLedger,
    summary: ImageUploadSummary,
) -> list[PricebookAsset]:
    """Every uploadable asset, least recently verified first.

    A blank ``last_verified`` sorts before any timestamp, so assets the ledger
    has never seen are attempted first — the fastest route to full coverage on a
    catalogue too large for one job, and the whole reason two bounded runs sweep
    twice as far as one bounded run repeated. Python's sort is stable, so equally
    stale assets keep the catalogue's own order and a pass with no ledger at all
    behaves exactly as it did before there was one.
    """
    assets: list[PricebookAsset] = []
    for record in records:
        asset = select_uploadable_asset(record)
        if asset is None:
            summary.no_image += 1
            continue
        assets.append(asset)
    assets.sort(key=lambda asset: ledger.last_verified(asset_ref(asset)) or "")
    return assets


def asset_ref(asset: PricebookAsset) -> str:
    """The ledger's name for this asset. Known without downloading anything."""
    return f"{asset.external_item_id}:{asset.identity}"


def _is_still_fresh(asset: PricebookAsset, ledger: ImageLedger, *, now: str) -> bool:
    """True when this asset provably needs no download at all.

    The pre-download half of the ledger check, and the answer to why the pass
    used to re-download 7,000 images to learn it had already sent them: the
    idempotency key hashes the payload, so ``ledger.has(key)`` cannot be asked
    until the bytes are in hand. This asks a cheaper question of the two facts
    that ARE known up front — when the ledger last confirmed this asset, and when
    ServiceTitan last modified the item that owns it.

    Every condition is required, and each one fails CLOSED (download it):

    - the ledger has confirmed this asset at least once;
    - ServiceTitan gave the item a ``modifiedOn`` (blank cannot prove anything);
    - that modification is no LATER than our confirmation, so no new bytes can
      have appeared since;
    - and the confirmation is recent enough that we are still willing to trust
      the ``modifiedOn`` contract at all (``REVERIFY_AFTER_DAYS``). Failing THIS
      one no longer means a download: it means a conditional fetch, which on a
      server that offers validators costs a request and a 304.

    Both timestamps are ISO-8601 UTC — ours is ``datetime.now(timezone.utc)``,
    ServiceTitan's is the same shape — so a string compare is a time compare. A
    malformed one compares as smaller and the asset is downloaded, which is the
    safe direction.
    """
    verified_at = ledger.last_verified(asset_ref(asset))
    if not verified_at or not asset.modified_on:
        return False
    if asset.modified_on > verified_at:
        return False
    cutoff = _reverify_cutoff(now)
    return cutoff is None or verified_at >= cutoff


def _reverify_cutoff(now: str) -> str | None:
    """``now`` minus ``REVERIFY_AFTER_DAYS``, or None if ``now`` is unparseable."""
    try:
        moment = datetime.fromisoformat(now)
    except ValueError:
        # A caller that handed us something unparseable gets the conservative
        # answer everywhere else in this module gets: no shortcut.
        return None
    return (moment - timedelta(days=REVERIFY_AFTER_DAYS)).isoformat()


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
    if _is_still_fresh(asset, ledger, now=now):
        # The download that never happens. The asset's existing keys go into
        # `seen_keys` by hand because no content hash was computed this run, and
        # a key absent from `seen_keys` is what `ImageLedger.keep` prunes —
        # without this, a converged catalogue would prune itself empty and
        # re-upload everything on the run after that.
        summary.revalidated += 1
        summary.already_uploaded += 1
        summary.seen_keys |= ledger.keys_for(asset_ref(asset))
        return True

    authenticated = is_storage_path(asset.source_url)
    if authenticated and summary.permission_denied:
        # Already told, once, that this tenant will not serve image bytes.
        return True

    ref = asset_ref(asset)
    stored = Validators(*ledger.validators_for(ref))
    summary.conditional.observe_asset(
        source_url=asset.source_url, has_asset_id=bool(asset.asset_id)
    )
    summary.fetched += 1
    fetched = _fetch(client, public, asset, summary, stored)
    if fetched is None:
        # `_fetch` sets `stopped` when the failure is about the CONNECTION to
        # ServiceTitan rather than about this one asset; carrying on would spend
        # the same doomed retry budget on every remaining asset and can run the
        # whole workflow past its `timeout-minutes`.
        return summary.stopped is None

    if fetched.payload is None:
        # 304 NOT MODIFIED. The server checked our validator against the live
        # bytes and said "nothing new" — which is the same conclusion the old
        # weekly re-download reached, after moving the whole image to reach it.
        # No bytes, no content hash, no upload; just a fresh verification, keyed
        # on the ASSET rather than on a content hash we did not compute.
        summary.not_modified += 1
        summary.already_uploaded += 1
        summary.seen_keys |= ledger.keys_for(ref)
        ledger.verify_ref(ref, now)
        return True

    payload = fetched.payload
    if len(payload) > MAX_IMAGE_BYTES:
        summary.too_large += 1
        logger.info("pricebook image over the %d byte cap; skipped", MAX_IMAGE_BYTES)
        return True

    content_type = sniff_content_type(payload)
    if content_type is None:
        # TrueQuote would 422 these (`image_content_mismatch`); no point sending.
        # What ARRIVED is recorded, because "unsupported" alone cannot tell an
        # HTML login page from a GIF and those ask for opposite fixes.
        _record_unsupported(summary, asset, payload, fetched)
        return True

    key = idempotency_key(asset, payload)
    summary.seen_keys.add(key)
    if ledger.has(key):
        summary.already_uploaded += 1
        # Re-stamp it so it sorts to the BACK of the next pass. Leave the old
        # timestamp and this asset is re-downloaded first on every run for ever,
        # and the assets behind it are never reached — which is precisely the
        # shape run 35130164187 was stuck in.
        #
        # The validators are stored on the way past even though these bytes were
        # already delivered: this is the run that TEACHES the ledger what to
        # quote back, so the NEXT weekly re-verification of this asset can be a
        # 304 instead of a download.
        ledger.verify(key, now, etag=fetched.etag, last_modified=fetched.last_modified)
        return True

    if len(asset.identity) > _MAX_TEXT_FIELD:
        summary.unsupported += 1
        _count_shape(summary, "identity_too_long")
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
                asset_ref=ref,
                storage_path=result.storage_path,
                verified_at=now,
                etag=fetched.etag,
                last_modified=fetched.last_modified,
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


def _count_shape(summary: ImageUploadSummary, shape: str) -> None:
    summary.unsupported_shapes[shape] = summary.unsupported_shapes.get(shape, 0) + 1


def _record_unsupported(
    summary: ImageUploadSummary,
    asset: PricebookAsset,
    payload: bytes,
    fetched: _Fetched,
) -> None:
    """Count the rejection, and describe it while the detail budget allows.

    INFORMATION ONLY. Nothing here decides anything: the asset is refused
    either way, and the declared `Content-Type` is recorded precisely because
    it is NOT trusted — a server claiming `image/jpeg` over `<!DOCTYPE html` is
    the finding, not a reason to accept the bytes.

    Everything printed is bounded and sanitised: the url loses any query string
    (it may be presigned) and any userinfo, the body shows at most
    ``HEX_PREVIEW_BYTES`` bytes of hex and ``ASCII_PREVIEW_CHARS`` characters
    with every control byte replaced, and the declared content type is capped.
    No whole body ever reaches a log.
    """
    summary.unsupported += 1
    _count_shape(summary, payload_shape(payload))
    if summary.unsupported_detail_logged >= UNSUPPORTED_DETAIL_LIMIT:
        return
    summary.unsupported_detail_logged += 1
    logger.warning(
        "pricebook image REJECTED by the byte sniff (not PNG/JPEG/WEBP): "
        "item=%s asset=%s source=%s http_status=%s declared_content_type=%s %s",
        asset.external_item_id,
        redact_source_url(asset.identity),
        redact_source_url(asset.source_url),
        fetched.status_code or "?",
        _capped_declared(fetched.content_type),
        describe_rejected_payload(payload),
    )
    if summary.unsupported_detail_logged == UNSUPPORTED_DETAIL_LIMIT:
        logger.warning(
            "further rejected pricebook images will not be described individually "
            "(cap %d per run); the run summary's `unsupported_shapes` keeps counting "
            "every one of them.",
            UNSUPPORTED_DETAIL_LIMIT,
        )


def _capped_declared(value: str) -> str:
    """The response's own `Content-Type`, bounded and control-free, or `none`."""
    if not value:
        return "none"
    cleaned = "".join(ch if 0x20 <= ord(ch) < 0x7F else "." for ch in value[:80])
    return cleaned


@dataclass(frozen=True)
class _Fetched:
    """The outcome of one asset fetch that reached a status.

    ``payload is None`` means 304 Not Modified: the server confirmed our copy
    and sent no body. Anything else is bytes.
    """

    payload: bytes | None
    etag: str = ""
    last_modified: str = ""
    # What the server CLAIMED the body was, and the status it came back on.
    # Carried for diagnostics only — the upload's content type is still sniffed
    # from the bytes (`assets.sniff_content_type`), never read from here.
    content_type: str = ""
    status_code: int = 0


def _fetch(
    client: ServiceTitanClient,
    public: httpx.Client,
    asset: PricebookAsset,
    summary: ImageUploadSummary,
    stored: Validators,
) -> _Fetched | None:
    """One asset, fetched CONDITIONALLY where we have something to quote back.

    Returns None when it could not be fetched at all (already counted).

    ``stored`` carries the ``ETag``/``Last-Modified`` the ledger kept from the
    last successful fetch of this asset. When it is empty — a first fetch, a
    pre-upgrade ledger row, or a server that offers no validators — the headers
    are empty too and this is precisely the unconditional GET it has always
    been. That is the fallback, and it is the default: nothing here assumes
    ServiceTitan implements conditional requests, and the one thing the pass
    insists on is COUNTING what came back (``summary.conditional``).
    """
    conditional_headers = stored.headers()
    try:
        if is_storage_path(asset.source_url):
            file = client.get_file(
                _IMAGES_MODULE,
                _IMAGES_RESOURCE,
                params={"path": asset.source_url},
                headers=conditional_headers or None,
            )
            status, body = file.status_code, file.content
            etag, last_modified = file.etag, file.last_modified
            declared = file.content_type
        else:
            resp = public.get(asset.source_url, headers=conditional_headers)
            # 304 is a success, and `raise_for_status` agrees (it raises only on
            # 4xx/5xx) — but say so explicitly, because a future httpx that
            # treated 3xx as an error here would silently turn every cheap
            # verification into a `download_failed`.
            if resp.status_code != 304:
                resp.raise_for_status()
            status, body = resp.status_code, resp.content
            etag = resp.headers.get("etag")
            last_modified = resp.headers.get("last-modified")
            declared = resp.headers.get("content-type")
        summary.conditional.observe_response(
            conditional=bool(conditional_headers),
            status_code=status,
            etag=etag,
            last_modified=last_modified,
        )
        if status == 304:
            return _Fetched(payload=None)
        return _Fetched(
            payload=body,
            etag=etag or "",
            last_modified=last_modified or "",
            content_type=declared or "",
            status_code=status,
        )
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
    except TransportError as exc:
        summary.download_failed += 1
        if is_storage_path(asset.source_url):
            # ServiceTitan itself is unreachable, not this one image. The same
            # failure awaits every other authenticated asset, and each one now
            # costs a full retry budget of timeouts — five of them exceed the
            # workflow's timeout-minutes on their own. Stop the PASS, the way a
            # 403 already does; the next scheduled run retries every asset.
            summary.stopped = f"ServiceTitan images endpoint unreachable: {exc}"
            logger.warning(
                "pricebook image download could not reach ServiceTitan (%s); stopping "
                "the image pass rather than timing the run out one asset at a time. "
                "Every pricebook tab was still written.",
                exc,
            )
        else:
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

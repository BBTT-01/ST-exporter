"""Pure asset rules for the image upload lane — no HTTP, no Sheets.

Every rule here is a mirror of a rule TrueQuote's receiving side already
enforces. Where that is so, the TrueQuote source is named, because the point of
this module is to *match* their validator rather than to have an opinion:

- ``is_displayable`` / ``select_uploadable_asset`` mirror
  ``isDisplayablePricebookImage`` / ``selectDefaultPricebookImage``
  (``packages/servicetitan/src/server.ts:100,118``).
- ``sniff_content_type`` mirrors ``bytesMatchImageContentType``
  (``apps/admin/lib/integrations/hosted-pricebook-image.ts:34``): TrueQuote
  rejects a POST whose bytes do not match the declared ``Content-Type``, so the
  declaration is derived from the bytes rather than trusted from ServiceTitan's
  response header.
- ``MAX_IMAGE_BYTES`` mirrors ``MAX_CRM_IMAGE_BYTES``
  (``apps/admin/lib/integrations/crm-pricebook-assets.ts``, 8 MiB).

**Exactly one asset per item is uploaded.** TrueQuote's receiving route passes
``is_primary: true`` for whatever it is handed, and
``reconcile_crm_pricebook_assets`` first clears ``is_primary`` for every asset of
that item before upserting the incoming one
(``supabase/migrations/20260729000800_persist_crm_asset_storage_paths.sql``). So
a second upload for the same item does not add a second image, it *moves* the
item's primary. Sending the one asset their own ``selectDefaultPricebookImage``
would have chosen is the only way a hosted item ends up looking like a
direct-mode item.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from st_exporter.format import to_cell_text

# TrueQuote's cap, matched locally so a doomed POST is never made.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# The three content types TrueQuote's `normalizedCrmImageContentType` accepts.
ContentType = str
_JPEG = "image/jpeg"
_PNG = "image/png"
_WEBP = "image/webp"

# `isServiceTitanPricebookImagePath` (packages/servicetitan/src/server.ts:93),
# transcribed. Narrow on purpose: a storage path is handed straight back to
# ServiceTitan's images endpoint as a query value.
_STORAGE_PATH = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9._-]+)+$")

# `positiveInteger` in route.ts:119 — up to 18 digits, must be > 0.
_EXTERNAL_ITEM_ID = re.compile(r"^(?!0+$)\d{1,18}$")


@dataclass(frozen=True)
class PricebookAsset:
    """One uploadable asset, already bound to the item it belongs to."""

    external_item_id: str
    asset_id: str | None
    source_url: str
    filename: str | None
    alias: str | None
    asset_type: str | None
    is_default: bool

    @property
    def identity(self) -> str:
        """``asset_id`` when ServiceTitan supplies one, else the source url.

        The same identity rule as ``st_exporter.pricebook.asset_identifier`` and
        as TrueQuote's ``crmAssetKey`` — which hashes the url when there is no
        id, so the two agree on *which* asset this is even though they spell the
        key differently.
        """
        return self.asset_id or self.source_url


def is_storage_path(value: str) -> bool:
    """True for an authenticated ServiceTitan path like ``Images/Pricebook/x.jpg``."""
    return ".." not in value and bool(_STORAGE_PATH.match(value))


def is_storable(value: str) -> bool:
    """TrueQuote's ``isStorableSourceUrl``: an HTTPS url or a storage path."""
    return value.startswith("https://") or is_storage_path(value)


def is_displayable(asset: dict[str, Any]) -> bool:
    """TrueQuote's ``isDisplayablePricebookImage``, transcribed.

    A blank ``type`` passes — ServiceTitan leaves it null on plenty of real
    images, and treating that as "not an image" would drop them silently.
    """
    url = to_cell_text(asset.get("url")).strip()
    if not is_storable(url):
        return False
    asset_type = to_cell_text(asset.get("type")).strip().lower()
    return not asset_type or "image" in asset_type or "photo" in asset_type


def select_uploadable_asset(record: dict[str, Any]) -> PricebookAsset | None:
    """The one asset to upload for this item, or None if it has no usable image.

    Order mirrors ``selectDefaultPricebookImage``: ``isDefault`` first, then the
    ``id|fileName|alias|url`` tuple. TrueQuote sorts that tuple with
    ``localeCompare`` and this sorts by code point; the two can disagree only
    when an item has several equally-default images whose ids differ by case or
    accent, which changes *which* image is primary and never whether one is.
    """
    external_item_id = to_cell_text(record.get("id")).strip()
    # TrueQuote parses this with /^\d{1,18}$/ and 422s anything else
    # (route.ts:120). An item whose id is not a plain positive integer has no
    # uploadable image as far as the endpoint is concerned, so it is dropped
    # here rather than sent to be refused.
    if not _EXTERNAL_ITEM_ID.match(external_item_id):
        return None

    candidates = [
        asset
        for asset in record.get("assets") or []
        if isinstance(asset, dict) and is_displayable(asset)
    ]
    if not candidates:
        return None

    chosen = min(candidates, key=lambda a: (not _is_default(a), _order_key(a)))
    return PricebookAsset(
        external_item_id=external_item_id,
        asset_id=_text_or_none(chosen.get("id")),
        source_url=to_cell_text(chosen.get("url")).strip(),
        filename=_text_or_none(chosen.get("fileName")),
        alias=_text_or_none(chosen.get("alias")),
        asset_type=_text_or_none(chosen.get("type")),
        is_default=_is_default(chosen),
    )


def sniff_content_type(payload: bytes) -> ContentType | None:
    """The content type TrueQuote's byte check will agree with, or None.

    Derived from the magic bytes rather than from ServiceTitan's response
    header, because TrueQuote rejects (422 ``image_content_mismatch``) any POST
    whose body does not start with the signature for the declared type. A header
    saying ``image/jpeg`` over PNG bytes is therefore worse than no header.
    """
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return _PNG
    if payload.startswith(b"\xff\xd8\xff"):
        return _JPEG
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return _WEBP
    return None


def idempotency_key(asset: PricebookAsset, payload: bytes) -> str:
    """Stable key for "these exact bytes, for this exact asset, already sent".

    Deterministic in the asset's identity *and* its content: nothing about the
    clock, the run, or the ordering enters it. Two consequences, both wanted:

    - A re-run that downloads byte-identical images produces identical keys, so
      the local ledger skips every upload and no bytes cross the wire twice.
    - A genuinely changed image produces a new key and IS re-sent — TrueQuote
      upserts the same storage path, so the item's picture updates in place.

    The content hash is ours alone. TrueQuote derives its own dedupe key from
    ``(external_item_id, asset_id | sha256(source_url))`` and stores at a path
    derived from ``sha256(source_url)``, both content-independent — see the note
    in ``client.py`` about the header it does not read.
    """
    digest = hashlib.sha256()
    digest.update(asset.external_item_id.encode())
    digest.update(b"\x00")
    digest.update(asset.identity.encode())
    digest.update(b"\x00")
    digest.update(asset.source_url.encode())
    digest.update(b"\x00")
    digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def _order_key(asset: dict[str, Any]) -> str:
    return "\x00".join(
        to_cell_text(asset.get(field)) for field in ("id", "fileName", "alias", "url")
    )


def _is_default(asset: dict[str, Any]) -> bool:
    return asset.get("isDefault") is True


def _text_or_none(value: Any) -> str | None:
    text = to_cell_text(value).strip()
    return text or None

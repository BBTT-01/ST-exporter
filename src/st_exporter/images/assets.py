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
from urllib.parse import urlsplit

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
    # The owning ITEM's ``modifiedOn``, carried here because it is the only
    # cheap validator the pass has: it is known from the catalogue listing,
    # before a single byte is downloaded. See ``upload._is_still_fresh`` — an
    # asset whose item has not been modified since the ledger last confirmed it
    # is skipped without a download, which is the difference between a pass that
    # converges and one that re-downloads 7,000 images on every run to
    # rediscover they were already sent.
    #
    # Blank when ServiceTitan omits it, and blank means "cannot prove
    # freshness", never "fresh": the skip requires a non-empty value. Defaulted
    # so every existing construction of this dataclass keeps working.
    modified_on: str = ""

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
        modified_on=to_cell_text(record.get("modifiedOn")).strip(),
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


# The floor below which a byte-valid image is treated as a PLACEHOLDER rather
# than as a picture.
#
# ServiceTitan's web image endpoint takes a `default=Default%2F1.png` parameter
# and, when the caller may not see the real asset, answers **200 OK** with a
# blank placeholder instead of a 404. One measured by hand was a 179x179 WebP of
# 246 bytes: a perfectly well-formed RIFF/WEBP file that sniffs clean, uploads
# clean, and shows the contractor an empty grey square. Sixteen thousand of
# those, reported as a clean run, is the silent success this lane keeps hitting.
#
# 1 KiB, not 246. The threshold is not the observed sample — one tenant's
# placeholder is one data point and the next could be 300 bytes — it is the
# floor below which no PHOTOGRAPH exists. A lossy-compressed image carrying any
# real detail costs on the order of a kilobyte before it carries anything: the
# smallest real pricebook assets seen are 2-4 KiB, and a deliberately tiny
# 64x64 product thumbnail still lands above 1 KiB. Everything under it is a
# solid fill, a 1x1, or a spacer. 1 KiB is ~4x the observed placeholder — wide
# enough that a differently-sized placeholder is still caught — and still well
# under the smallest genuine asset, so it cannot silently drop a real image.
#
# This is deliberately NOT part of `sniff_content_type`: the sniff answers "what
# format is this", stays exactly as narrow as TrueQuote's, and must keep
# answering it. This answers a different question — "is this a picture of
# anything" — and its rejections are counted under their own name.
MIN_PLAUSIBLE_IMAGE_BYTES = 1024


def is_placeholder_image(payload: bytes) -> bool:
    """True for a byte-valid image too small to contain a real picture.

    Size alone, on purpose. A content-hash blocklist would only catch the exact
    placeholder we have already seen, and ServiceTitan serves a different one
    per `default=` value and per requested `size=`; the floor catches every one
    of them, including the ones nobody has met yet. The sha256 of each rejected
    payload is logged so a hash rule can be added later if the floor ever proves
    too blunt for a specific tenant.
    """
    return len(payload) < MIN_PLAUSIBLE_IMAGE_BYTES


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


# ---------------------------------------------------------------------------
# Diagnostics for a payload the sniff REFUSED.
#
# `sniff_content_type` returning None is the single most opaque outcome the
# image pass has: the bytes arrived, they were discarded, and the run said
# `unsupported=N` and nothing else. Run 35142282102 on `BBTT-01/tr-doorservpro`
# rejected all five assets it fetched that way, and the counter alone cannot
# tell "ServiceTitan handed us an HTML error page" from "this tenant's images
# are GIFs".
#
# Everything below is INFORMATION ONLY. Nothing here widens what is accepted:
# the sniff is unchanged, the declared `Content-Type` is still never trusted,
# and a payload these functions can name is still refused.
# ---------------------------------------------------------------------------

# How many leading bytes are ever rendered. Both caps are small on purpose: a
# rejected payload may be an HTML body containing anything at all, so the line
# shows a fingerprint, never a document.
HEX_PREVIEW_BYTES = 16
ASCII_PREVIEW_CHARS = 32

_MAGIC_SHAPES: tuple[tuple[bytes, str], ...] = (
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"%PDF", "pdf"),
    (b"\x00\x00\x01\x00", "ico"),
    (b"PK\x03\x04", "zip"),
)

_TEXT_SHAPES: tuple[tuple[str, str], ...] = (
    ("<!doctype html", "html"),
    ("<html", "html"),
    ("<svg", "svg"),
    ("<?xml", "xml"),
    ("{", "json"),
    ("[", "json"),
)


def payload_shape(payload: bytes) -> str:
    """A one-word guess at what a rejected payload actually is.

    A PREFIX CHECK, not a parser: it never decodes the body, never allocates
    more than the first few dozen bytes, and is wrong in the harmless direction
    (``unknown``) whenever it is not sure. The point is that one summary line
    can say ``html:5`` — "we are not downloading images at all" — or ``gif:5``
    — "a format we do not accept" — which are opposite diagnoses.
    """
    if not payload:
        return "empty"
    head = payload[:64].lstrip(b"\xef\xbb\xbf").lstrip()
    for magic, shape in _MAGIC_SHAPES:
        if payload.startswith(magic):
            return shape
    if payload[4:8] == b"ftyp":
        return "heic"
    lowered = head[:32].lower()
    for prefix, shape in _TEXT_SHAPES:
        if lowered.startswith(prefix.encode()):
            return shape
    if head and all(0x20 <= byte < 0x7F or byte in (0x09, 0x0A, 0x0D) for byte in head):
        return "text"
    return "unknown"


def payload_hex_prefix(payload: bytes, limit: int = HEX_PREVIEW_BYTES) -> str:
    """The first ``limit`` bytes as spaced hex. Empty payload renders as ``-``."""
    return payload[:limit].hex(" ") or "-"


def payload_ascii_preview(payload: bytes, limit: int = ASCII_PREVIEW_CHARS) -> str:
    """A bounded, control-free rendering of the leading bytes.

    Every byte outside printable ASCII becomes ``.`` — including newline and
    tab, so the result can never break a log line or smuggle a terminal escape
    — and at most ``limit`` characters are produced whatever arrives.
    """
    return "".join(chr(byte) if 0x20 <= byte < 0x7F else "." for byte in payload[:limit])


def redact_source_url(url: str) -> str:
    """The asset url as it may be logged: no credentials, no query, no fragment.

    An asset url can be presigned (see ``images/conditional.py``), so its query
    string IS a credential. The path is kept because it is what identifies the
    asset to a human reading the log; everything that could carry a secret is
    replaced by a marker that still says one was there.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - urlsplit is extremely permissive
        return "<unparseable-url>"
    if not parts.scheme and not parts.netloc:
        # A ServiceTitan storage path like `Images/Pricebook/x.jpg`. No
        # credential can hide in one — `is_storage_path` forbids `?` and `@`.
        return url.split("?", 1)[0]
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = "<redacted>@" if parts.username or parts.password else ""
    rendered = f"{parts.scheme}://{userinfo}{host}{parts.path}"
    if parts.query:
        rendered += "?<redacted>"
    return rendered


def describe_rejected_payload(payload: bytes) -> str:
    """``shape=… bytes=… hex=… ascii=…`` for one payload the sniff refused."""
    return (
        f"shape={payload_shape(payload)} bytes={len(payload)} "
        f"hex={payload_hex_prefix(payload)} "
        f"ascii={payload_ascii_preview(payload)!r}"
    )

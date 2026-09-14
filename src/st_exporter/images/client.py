"""HTTP client for TrueQuote's pricebook image endpoint.

The wire format is not ours to choose — it is exactly what
``apps/admin/app/api/outbox/pricebook-image/route.ts`` reads:

    POST {outbox_base_url}/pricebook-image
      ?external_item_id=<positive integer, <=18 digits>
      &source_url=<the ServiceTitan asset url or storage path>
      [&asset_id=<string>][&filename=<string>][&alias=<string>]
      [&asset_type=<string>][&is_default=true]
    Authorization: Bearer <Machine Token, scope image_upload>
    Content-Type: image/jpeg | image/png | image/webp
    <raw image bytes as the body>

All metadata is in the QUERY STRING; the body is the bytes and nothing else —
no multipart, no base64, no JSON envelope (route.ts:21-63, readCappedBody at
:76). ``is_default`` is read as the literal string ``true`` (route.ts:62), so it
is only sent when true. The company is resolved server-side from the token
(``machine-token.ts:176``) and is never sent by us.

**Idempotency.** TrueQuote's route reads no idempotency field of any kind. Its
dedupe is intrinsic: the storage path is ``sha256(source_url)`` and the row key
is ``(external_item_id, asset_id | sha256(source_url))``, uploaded with
``upsert: true`` (hosted-pricebook-image.ts:64-87), so a replayed POST overwrites
the same object and the same row — at-least-once safe, but only *after* the
bytes have crossed the wire. The ``Idempotency-Key`` header this client sends is
therefore, today, **read by nobody**: it is sent so the value is on the wire if
TrueQuote ever wants it, and the saving of the bytes themselves comes from our
own ledger (``ledger.py``), not from the server. See the report/KNOWN_UNVERIFIED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

_DEFAULT_TIMEOUT = 60.0
_PATH = "/pricebook-image"


@dataclass(frozen=True)
class ImageUploadAccepted:
    """A 200 from TrueQuote: the bytes are stored."""

    asset_key: str
    storage_path: str
    # 'stored' on first sight of this asset, 'replaced' when a row already
    # existed (route.ts:72 — it reports whether a ROW existed, not whether the
    # bytes differed, so it is not a "we already had these bytes" signal).
    status: str


@dataclass(frozen=True)
class ImageUploadRejected:
    """A non-200. ``retryable`` decides whether the run gives up on this pass.

    ``status_code`` is ``NO_HTTP_STATUS`` (0) when the request never reached a
    status at all — a DNS failure, a refused connection, a timeout.
    """

    status_code: int
    error: str
    retryable: bool

    @property
    def kind(self) -> Literal["retryable", "permanent"]:
        return "retryable" if self.retryable else "permanent"


ImageUploadResult = ImageUploadAccepted | ImageUploadRejected

# A rejection carrying status 0 is a transport failure — no HTTP status was ever
# received. It is always retryable: nothing about the ASSET was judged.
NO_HTTP_STATUS = 0

# 422 is TrueQuote saying "this asset is not acceptable" — the same bytes will
# be rejected again next run, so it is counted and not retried. 401/429/503 are
# about the connection, not the asset: they end the pass and the next scheduled
# run tries the same assets again.
_RETRYABLE_STATUSES = frozenset({401, 408, 429, 500, 502, 503, 504})


class TrueQuoteImageClient:
    """Wraps ``POST {base}/pricebook-image``."""

    def __init__(self, base_url: str, machine_token: str) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=_DEFAULT_TIMEOUT,
            headers={"Authorization": f"Bearer {machine_token}"},
        )

    def close(self) -> None:
        self._http.close()

    def upload(
        self,
        *,
        external_item_id: str,
        source_url: str,
        content_type: str,
        payload: bytes,
        idempotency_key: str,
        asset_id: str | None = None,
        filename: str | None = None,
        alias: str | None = None,
        asset_type: str | None = None,
        is_default: bool = False,
    ) -> ImageUploadResult:
        params: dict[str, str] = {
            "external_item_id": external_item_id,
            "source_url": source_url,
        }
        for key, value in (
            ("asset_id", asset_id),
            ("filename", filename),
            ("alias", alias),
            ("asset_type", asset_type),
        ):
            if value:
                params[key] = value
        if is_default:
            params["is_default"] = "true"

        try:
            resp = self._http.post(
                _PATH,
                params=params,
                content=payload,
                headers={
                    "Content-Type": content_type,
                    "Idempotency-Key": idempotency_key,
                },
            )
        except httpx.HTTPError as exc:
            # A DNS failure or a read timeout is the same KIND of fact as a 503:
            # TrueQuote is unreachable right now, and the next scheduled run
            # retries these bytes. Returning it as a retryable rejection rather
            # than raising is what keeps the promise this module's docstring
            # makes — the image pass ends, the pricebook run does not.
            return ImageUploadRejected(
                status_code=NO_HTTP_STATUS,
                error=f"transport_error: {type(exc).__name__}: {exc}",
                retryable=True,
            )

        if resp.status_code == 200:
            body = _json_object(resp)
            return ImageUploadAccepted(
                asset_key=str(body.get("asset_key", "")),
                storage_path=str(body.get("storage_path", "")),
                status=str(body.get("status", "")),
            )

        return ImageUploadRejected(
            status_code=resp.status_code,
            error=str(_json_object(resp).get("error") or f"http_{resp.status_code}"),
            retryable=resp.status_code in _RETRYABLE_STATUSES,
        )


def _json_object(resp: httpx.Response) -> dict[str, object]:
    """The response body as a dict, or ``{}`` — a proxy's HTML 503 is not JSON."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}

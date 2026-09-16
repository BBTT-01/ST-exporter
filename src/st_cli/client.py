"""Base HTTP client wrapping httpx with auth, retry, and error mapping."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from st_cli.auth import TokenManager
from st_cli.config import Settings
from st_cli.exceptions import APIError, NotFoundError, RateLimitError, TransportError

_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds


@dataclass(frozen=True)
class FetchedFile:
    """One file response, WITH the two headers a conditional re-fetch needs.

    ``get_bytes`` throws the response headers away, which is fine for a caller
    that always wants the bytes. A caller that wants to ask "have these bytes
    changed?" needs three more facts: the status (304 means "no", and carries no
    body at all), and the ``ETag`` / ``Last-Modified`` validators to quote back
    next time. Those are the only headers kept, deliberately — this is not a
    general response object.
    """

    status_code: int
    content: bytes
    content_type: str | None
    etag: str | None
    last_modified: str | None

    @property
    def not_modified(self) -> bool:
        """True for a 304: the server confirmed our copy without sending bytes."""
        return self.status_code == 304

    @property
    def has_validator(self) -> bool:
        """True when the server offered something to quote back next time.

        False means conditional requests are inert for this asset and the caller
        must fall back to re-downloading it on whatever schedule it would have
        used before validators existed.
        """
        return bool(self.etag or self.last_modified)


def _is_retriable_transport(method: str, exc: httpx.HTTPError, *, idempotent: bool = False) -> bool:
    """Whether a request that never got a status may be sent AGAIN.

    A ``ReadTimeout`` or a ``RemoteProtocolError`` after a POST means the
    request very likely *reached* ServiceTitan and only the answer was lost —
    re-sending it creates a second booking, a second lead, a second job. The
    outbox writes its idempotency ledger only after ``perform`` returns, so a
    duplicate there cannot be deduplicated afterwards. Duplicating a real
    contractor's ServiceTitan record is far worse than failing a run, so:

    - a read (``GET``, which ``get_bytes`` also uses) is retried freely — it has
      no effect to duplicate;
    - a non-GET the CALLER has declared ``idempotent`` — meaning re-sending it
      cannot create or mutate anything — is retried on the same terms as a GET;
    - any other method is retried ONLY when the failure proves the request was
      never sent, i.e. the connection itself was never established
      (``ConnectError`` / ``ConnectTimeout``);
    - everything else raises ``TransportError`` immediately, which is the
      pre-branch behaviour and what every per-tab guard already handles.

    ``idempotent`` exists for exactly one caller: ServiceTitan's
    ``POST reporting/.../data``, which is a *read* whose parameters merely do
    not fit a query string. It is never a property of the verb and never a
    property of a create or an update — see ``ServiceTitanClient.post``.
    """
    if method.upper() == "GET" or idempotent:
        return True
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


class ServiceTitanClient:
    """HTTP client for ServiceTitan API v2."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token_manager = TokenManager(settings)
        self._http = httpx.Client(base_url=settings.api_base, timeout=30.0)

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_manager.get_token()}",
            "ST-App-Key": self._settings.app_key,
        }

    def _url(self, module: str, resource: str) -> str:
        return f"/{module}/v2/tenant/{self._settings.tenant_id}/{resource}"

    def get(self, module: str, resource: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", module, resource, params=params)

    def post(
        self,
        module: str,
        resource: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        *,
        idempotent: bool = False,
    ) -> Any:
        """POST and decode the JSON answer.

        :param idempotent: **"Re-sending this request cannot create or mutate
            anything."** Not "the endpoint is safe to call twice-ish", not "the
            server dedupes": a literal guarantee that the request has no effect
            at all, so replaying it after a lost answer is indistinguishable
            from a GET. Setting it on a create or an update — a booking, a lead,
            a job, a price push — re-enables the duplicate-write bug that
            ``_is_retriable_transport`` exists to prevent, and the duplicate
            lands in a real contractor's ServiceTitan tenant where nothing can
            take it back. There is exactly ONE caller in this repo,
            ``st_exporter.feeds.reporting.fetch_report_rows``, whose
            ``POST .../data`` runs a report; if you are adding a second, it must
            be a read wearing a POST for the same reason.
        """
        return self._request(
            "POST", module, resource, params=params, json_body=json_body, idempotent=idempotent
        )

    def patch(self, module: str, resource: str, json_body: dict[str, Any] | None = None) -> Any:
        return self._request("PATCH", module, resource, json_body=json_body)

    def put(self, module: str, resource: str, json_body: dict[str, Any] | None = None) -> Any:
        return self._request("PUT", module, resource, json_body=json_body)

    def delete(
        self,
        module: str,
        resource: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        return self._request("DELETE", module, resource, params=params, json_body=json_body)

    def get_bytes(
        self, module: str, resource: str, params: dict[str, Any] | None = None
    ) -> tuple[bytes, str | None]:
        """Raw response body + its ``Content-Type``, for endpoints returning a file.

        ``pricebook/v2/tenant/{id}/images?path=…`` answers with image bytes, not
        JSON, so ``get()``'s ``resp.json()`` would raise on it. Everything else —
        auth header, 401 refresh, 429 backoff, error mapping — is identical;
        only the decoding differs.
        """
        fetched = self.get_file(module, resource, params=params)
        return fetched.content, fetched.content_type

    def get_file(
        self,
        module: str,
        resource: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> FetchedFile:
        """``get_bytes`` plus the status and the two cache validators.

        ``headers`` is merged OVER the auth headers, which is what lets a caller
        send ``If-None-Match`` / ``If-Modified-Since``. A 304 is NOT an error —
        it is the cheapest possible success — so it travels back as a
        ``FetchedFile`` with an empty body rather than raising; only >= 400 maps
        onto the exception hierarchy, exactly as before.
        """
        resp = self._send("GET", module, resource, params=params, headers=headers)
        return FetchedFile(
            status_code=resp.status_code,
            content=resp.content,
            content_type=resp.headers.get("content-type"),
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )

    def _request(
        self,
        method: str,
        module: str,
        resource: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        idempotent: bool = False,
    ) -> Any:
        resp = self._send(
            method, module, resource, params=params, json_body=json_body, idempotent=idempotent
        )
        if resp.status_code == 204:
            return None
        return resp.json()

    def _send(
        self,
        method: str,
        module: str,
        resource: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        idempotent: bool = False,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue one API call and map failures onto the exception hierarchy.

        Returns the raw ``httpx.Response`` so callers can decode it as JSON
        (``_request``) or as bytes (``get_bytes``) — the retry/auth/error
        behaviour must not be duplicated per decoding.

        **Every** failure mode leaves here as an ``STCLIError``: a non-success
        status as an ``APIError`` subclass, and a request that never reached a
        status as a ``TransportError``. Nothing raw from ``httpx`` escapes, so
        ``except STCLIError`` anywhere upstream is a complete guard rather than
        one that holds until the network hiccups.
        """
        url = self._url(module, resource)
        retries = 0
        refreshed = False
        extra = dict(headers or {})

        while True:
            try:
                resp = self._http.request(
                    method,
                    url,
                    # Rebuilt each pass, because a 401 refresh replaces the token
                    # mid-loop. `extra` is merged over it so a caller-supplied
                    # header wins, and so it survives the refresh.
                    headers={**self._headers(), **extra},
                    params=params,
                    json=json_body,
                )
            except httpx.HTTPError as exc:
                # No status came back at all — DNS, connect, TLS, read timeout.
                # Raised as an STCLIError (never a bare httpx exception, which
                # once killed three already-fetched tabs) — but retried only
                # when re-sending is PROVABLY safe, see `_is_retriable_transport`.
                if (
                    _is_retriable_transport(method, exc, idempotent=idempotent)
                    and retries < _MAX_RETRIES
                ):
                    retries += 1
                    time.sleep(_BACKOFF_BASE * (2 ** (retries - 1)))
                    continue
                attempts = (
                    f"after {retries} retr{'y' if retries == 1 else 'ies'}"
                    if retries
                    else "and was not retried (a re-send could duplicate the write)"
                )
                raise TransportError(
                    f"{method} {url} failed without an HTTP response {attempts} "
                    f"({type(exc).__name__}: {exc})"
                ) from exc

            if resp.status_code == 401 and not refreshed:
                self._token_manager.force_refresh()
                refreshed = True
                continue

            if resp.status_code == 429 and retries < _MAX_RETRIES:
                retries += 1
                wait = _BACKOFF_BASE * (2 ** (retries - 1))
                time.sleep(wait)
                continue

            break

        if resp.status_code == 404:
            raise NotFoundError(resp.text)
        if resp.status_code == 429:
            raise RateLimitError(resp.text)
        if resp.status_code >= 400:
            raise APIError(resp.status_code, resp.text)

        return resp

"""Base HTTP client wrapping httpx with auth, retry, and error mapping."""

from __future__ import annotations

import time
from typing import Any

import httpx

from st_cli.auth import TokenManager
from st_cli.config import Settings
from st_cli.exceptions import APIError, NotFoundError, RateLimitError, TransportError

_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds


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
        resp = self._send("GET", module, resource, params=params)
        return resp.content, resp.headers.get("content-type")

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

        while True:
            try:
                resp = self._http.request(
                    method, url, headers=self._headers(), params=params, json=json_body
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

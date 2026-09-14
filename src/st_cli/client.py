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
    ) -> Any:
        return self._request("POST", module, resource, params=params, json_body=json_body)

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
    ) -> Any:
        resp = self._send(method, module, resource, params=params, json_body=json_body)
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
                # Retried on the same budget as a 429 (a timeout is far more
                # often a blip than a verdict), then raised as an STCLIError so
                # the per-tab guards in st_exporter can catch it. Letting a bare
                # httpx exception escape killed three already-fetched tabs.
                if retries < _MAX_RETRIES:
                    retries += 1
                    time.sleep(_BACKOFF_BASE * (2 ** (retries - 1)))
                    continue
                raise TransportError(
                    f"{method} {url} failed without an HTTP response after "
                    f"{retries} retr{'y' if retries == 1 else 'ies'} "
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

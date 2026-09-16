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

# How many 3xx hops one safe request may take before it is refused.
#
# ServiceTitan's `pricebook/v2/tenant/{id}/images?path=…` does not hand back the
# bytes: it answers `302` with an empty body and a `Location` pointing at the
# blob store the image actually lives in. Until this existed the exporter read
# that 302 as the response — a zero-byte payload, no `Content-Type`, no
# validator — and every single pricebook image was discarded by the byte sniff
# as `shape=empty` (run 35143576507 on `BBTT-01/tr-doorservpro`: `considered=5
# uploaded=0 unsupported=5 unsupported_shapes=empty:5`).
#
# Bounded rather than unbounded because a redirect loop must fail as one named
# asset failure, not as a hang that eats the workflow's `timeout-minutes`. One
# API hop plus a couple of CDN hops is the shape actually observed; 5 leaves
# room without ever letting a cycle run.
_MAX_REDIRECTS = 5

# Statuses a GET follows. 303 is included (it names GET explicitly) and so are
# 307/308, which preserve the method — which for a GET is the same thing.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Only safe methods are followed. A 3xx on a POST/PATCH/PUT/DELETE is left to
# the caller exactly as before: re-issuing a write against a new URL is the
# duplicate-write hazard `_is_retriable_transport` exists to refuse.
_REDIRECTABLE_METHODS = frozenset({"GET", "HEAD"})


def _same_origin(left: httpx.URL, right: httpx.URL) -> bool:
    """Scheme + host + effective port all equal — RFC 6454's origin, not the host."""
    return left.scheme == right.scheme and left.host == right.host and left.port == right.port


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

        A 3xx is followed (bounded by ``_MAX_REDIRECTS``, credentials dropped
        off-origin) before it gets here, so ``status_code`` is the status of the
        response that actually carried the bytes. ``pricebook/.../images`` does
        redirect: it answers 302 and points at the blob store.
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
        url: str | httpx.URL = self._url(module, resource)
        retries = 0
        refreshed = False
        extra = dict(headers or {})
        redirects = 0
        # False once a redirect has taken us off ServiceTitan's API origin. Our
        # credentials are scoped to that origin and to nowhere else, so they do
        # not travel: not the bearer token, not `ST-App-Key`. httpx's own
        # redirect handling drops `Authorization` cross-origin but knows nothing
        # about `ST-App-Key`, which is exactly why this is hand-rolled rather
        # than `follow_redirects=True` — an image 302 points at a blob host, and
        # a tenant's app key must not be handed to it.
        send_credentials = True

        while True:
            request_headers = {**self._headers(), **extra} if send_credentials else dict(extra)
            try:
                resp = self._http.request(
                    method,
                    url,
                    # Rebuilt each pass, because a 401 refresh replaces the token
                    # mid-loop. `extra` is merged over it so a caller-supplied
                    # header wins, and so it survives the refresh.
                    headers=request_headers,
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

            # Only OUR origin's 401 is about OUR token. A 401 from a blob host
            # we were redirected to is its own access control and refreshing a
            # ServiceTitan token cannot answer it.
            if resp.status_code == 401 and not refreshed and send_credentials:
                self._token_manager.force_refresh()
                refreshed = True
                continue

            if resp.status_code == 429 and retries < _MAX_RETRIES:
                retries += 1
                wait = _BACKOFF_BASE * (2 ** (retries - 1))
                time.sleep(wait)
                continue

            if method.upper() in _REDIRECTABLE_METHODS and resp.status_code in _REDIRECT_STATUSES:
                location = resp.headers.get("location", "").strip()
                if not location:
                    # A 3xx with nowhere to go is not a success with an empty
                    # body — which is precisely how it used to be read.
                    raise APIError(
                        resp.status_code,
                        f"{method} {url} answered {resp.status_code} with no Location header",
                    )
                if redirects >= _MAX_REDIRECTS:
                    raise APIError(
                        resp.status_code,
                        f"{method} {self._url(module, resource)} exceeded "
                        f"{_MAX_REDIRECTS} redirects (last hop: {resp.status_code})",
                    )
                redirects += 1
                target = resp.url.join(location)
                if send_credentials and not _same_origin(target, resp.url):
                    send_credentials = False
                url = target
                # The Location carries the whole query it wants — re-appending
                # ours would duplicate `path=` onto a presigned blob url and
                # invalidate its signature.
                params = None
                json_body = None
                continue

            break

        if resp.status_code == 404:
            raise NotFoundError(resp.text)
        if resp.status_code == 429:
            raise RateLimitError(resp.text)
        if resp.status_code >= 400:
            raise APIError(resp.status_code, resp.text)

        return resp

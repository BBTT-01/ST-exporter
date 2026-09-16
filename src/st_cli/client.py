"""Base HTTP client wrapping httpx with auth, retry, and error mapping."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import httpx

from st_cli.auth import TokenManager
from st_cli.config import Settings
from st_cli.exceptions import APIError, NotFoundError, RateLimitError, TransportError

#: Where this module says "I am asleep on purpose".
#:
#: `st_cli` had no logger at all, which was fine while the longest a 429 could
#: park a request was four seconds. It is not fine now that the client honours a
#: server-stated `Retry-After`: a single request can sit silent for a minute and a
#: half, and silence is indistinguishable from a hang to whoever is watching the
#: Actions log. `st_exporter.logging_setup.configure_logging` gives this logger
#: the same level and handler it gives `st_exporter`, so an exporter run shows
#: these lines; a bare `st` CLI invocation configures no logging and is unchanged.
logger = logging.getLogger("st_cli.client")

_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds

#: Only a rate-limit sleep at least this long earns a log line.
#:
#: The blind exponential curve (1s, 2s, 4s) is the ordinary noise of a busy
#: endpoint and narrating it would bury the run log — the pricebook image pass
#: alone can earn hundreds of those across its workers. A wait this long is
#: always a server-STATED one, which is the only kind long enough for a human to
#: mistake for a hang.
_RATE_LIMIT_LOG_THRESHOLD = 5.0  # seconds

#: The longest one 429 may park a request for, however long the server asks.
#:
#: The exponential curve above is 1s + 2s + 4s = SEVEN SECONDS of total patience,
#: which is right for an endpoint that throttles per-second and hopeless for one
#: that throttles per-minute. ServiceTitan's reporting endpoint allows roughly one
#: run of the same report per minute per tenant, and **each page of a report
#: counts as another run** — so page 2 of any multi-page report is throttled by
#: page 1, every time, on every tenant. Run 35158215902 on `BBTT-01/tr-doorservpro`:
#:
#:     HTTP 429 {"status":429,"title":"Rate limit is exceeded. Try again in 50
#:     seconds."}  (page 2, 11 seconds into the report)
#:
#: Seven seconds of backoff against a fifty-second ask fails 100% of the time, so
#: `reporting.jobCosts` could never have been written for a report long enough to
#: paginate — not on a dispatch, not on the six-hourly schedule.
#:
#: The server says how long it wants; the fix is to believe it rather than guess.
#: This cap exists only so a server that asks for an hour cannot park a run until
#: the GitHub runner SIGKILLs it (see `EXPORTER_JOB_TIMEOUT_MINUTES`) — a wait
#: longer than this is refused as a rate-limit failure, loudly, which the caller
#: already knows how to degrade from.
_MAX_RATE_LIMIT_WAIT = 90.0  # seconds

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


#: ServiceTitan states the wait in the RFC 7231 problem body, not only in a
#: header: `{"status":429,"title":"Rate limit is exceeded. Try again in 50
#: seconds."}`. Matched case-insensitively, integer or decimal.
_RETRY_AFTER_IN_BODY = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*second", re.IGNORECASE)


def retry_after_seconds(resp: httpx.Response) -> float | None:
    """How long the server asked us to wait, in seconds, or ``None``.

    Two sources, header first:

    1. ``Retry-After`` — RFC 7231 allows either a delay in seconds or an HTTP
       date, and both spellings are accepted because which one a given edge
       returns is not ours to choose.
    2. the response BODY. ServiceTitan's 429 carries the number in its problem
       document (*"Rate limit is exceeded. Try again in 50 seconds."*) and does
       not always set the header, so reading only the header is the same as
       reading nothing on the endpoint that needs this most.

    Never returns a negative wait: an ``Retry-After`` date already in the past
    means "now", and a negative sleep would raise.
    """
    header = resp.headers.get("retry-after", "").strip()
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    try:
        body = resp.text
    except Exception:  # pragma: no cover - a body that cannot be decoded is not a wait
        return None
    match = _RETRY_AFTER_IN_BODY.search(body or "")
    if match:
        return max(0.0, float(match.group(1)))
    return None


class ServiceTitanClient:
    """HTTP client for ServiceTitan API v2.

    Safe to share across threads: ``httpx.Client`` is, ``TokenManager`` is made
    so explicitly, and every other piece of per-request state below is a local.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token_manager = TokenManager(settings)
        self._http = httpx.Client(
            base_url=settings.api_base,
            timeout=30.0,
            # Enough to let a concurrent caller (the image pass) actually use
            # its workers, and low enough that no caller can open an unbounded
            # number of sockets against one tenant. httpx's own default is 100;
            # this is narrower on purpose.
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        # Called with the seconds this client is ABOUT to sleep, every time it
        # backs off a 429. Optional, and None by default, so nothing changes for
        # the CLI or for any single-threaded caller.
        #
        # It exists because a per-request exponential backoff is not a rate
        # limiter: under concurrency, N workers each back off on their own
        # clock, wake together and hit the endpoint again as a wave. A caller
        # that holds a shared governor (`st_exporter.images.pacing`) sets this
        # so the 429 one worker earned slows down ALL of them — which is the
        # difference between a backoff and a rate limit.
        self.on_rate_limited: Callable[[float], None] | None = None

    def close(self) -> None:
        self._http.close()

    def _notify_rate_limited(self, wait: float) -> None:
        """Tell a shared governor, if one is listening, that we were throttled.

        Never lets the observer's own failure change what the request does: a
        governor is an optimisation over the backoff that is about to happen
        anyway.
        """
        observer = self.on_rate_limited
        if observer is None:
            return
        try:
            observer(wait)
        except Exception:  # noqa: BLE001 - an observer may not break a request
            pass

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
                # The server's own number first, the blind curve only when it
                # did not give one. A 429 that says "try again in 50 seconds"
                # and is retried 7 seconds later is a request that was never
                # going to succeed.
                asked = retry_after_seconds(resp)
                wait = asked if asked is not None else _BACKOFF_BASE * (2 ** (retries - 1))
                if wait > _MAX_RATE_LIMIT_WAIT:
                    # Refuse rather than park the whole run: a caller that skips
                    # one tab is recoverable, a job killed by the runner loses
                    # everything it had already done.
                    raise RateLimitError(
                        f"{method} {url} was rate-limited and the server asked for "
                        f"{wait:.0f}s, beyond the {_MAX_RATE_LIMIT_WAIT:.0f}s this client "
                        f"will wait on one request. Not waiting. Detail: {resp.text}"
                    )
                if wait >= _RATE_LIMIT_LOG_THRESHOLD:
                    # Said BEFORE the sleep, not after: a line that appears once
                    # the wait is over is no use to somebody deciding whether to
                    # cancel a run that has printed nothing for five minutes.
                    #
                    # The ServiceTitan resource, never `url`: `url` is rebound to
                    # the redirect target on an image fetch, and that is a
                    # PRESIGNED blob address whose query string is a credential.
                    logger.info(
                        "rate-limited by ServiceTitan on %s %s — waiting %.0fs "
                        "(%s; retry %d of %d). This is a throttle, not a hang.",
                        method,
                        self._url(module, resource),
                        wait,
                        "the server asked for this" if asked is not None else "no stated wait",
                        retries,
                        _MAX_RETRIES,
                    )
                self._notify_rate_limited(wait)
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

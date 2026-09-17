"""HTTP conditional-request plumbing for the image pass, and the measurement.

NOBODY HAS SEEN SERVICETITAN ANSWER A CONDITIONAL REQUEST
=========================================================

This module is written to be *useless safely*. Every function here degrades to
"no validators, download it" — the exact behaviour the pass had before
conditional requests existed — so a tenant whose CDN strips ``ETag`` loses
nothing, and a tenant whose asset urls are signed per request (so the validator
can never match) loses nothing either.

What it DOES buy, unconditionally, is the measurement. ``ConditionalReport``
counts what came back on the wire and renders one line per run, so a single live
run answers the questions no fixture can:

- do downloads carry an ``ETag`` or a ``Last-Modified`` at all, and on which of
  the two sources — ServiceTitan's authenticated ``pricebook/.../images``
  endpoint, or the public CDN urls;
- when we quote one back, does anything actually answer 304;
- and are the urls *signed*, which would make both questions moot because the
  url — and with it the ledger's own asset reference — changes every listing.

Read the line, then decide. Until it has been read, nothing in the pass assumes
a single one of these answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

# Query parameters that mean "this url carries its own credential and will be
# spelled differently next time". Deliberately a NAMED list rather than "has a
# query string": plenty of perfectly stable CDN urls carry `?v=3`.
#
# Covers the three signers a ServiceTitan asset url has been seen or is likely
# to be handed off to: S3 / CloudFront presigned (`X-Amz-*`, `AWSAccessKeyId`,
# `Expires`, `Signature`, `Key-Pair-Id`), Azure Blob SAS (`sig`, `se`, `sv`,
# `sp`, `st`), and Google Cloud Storage signed urls (`X-Goog-*`).
_SIGNED_QUERY_PARAMS = frozenset(
    {
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "awsaccesskeyid",
        "signature",
        "key-pair-id",
        "policy",
        "expires",
        "sig",
        "se",
        "sv",
        "sp",
        "st",
        "x-goog-signature",
        "x-goog-credential",
        "token",
    }
)

# `W/"abc"` — a weak validator. Still perfectly usable for `If-None-Match`; it
# only promises semantic rather than byte equality. Counted separately because
# if a tenant's 304s all come from weak tags, "unchanged" means "the server
# thinks so", and a sceptic will want to know that before trusting it.
_WEAK_ETAG = re.compile(r'^\s*W/"')


@dataclass(frozen=True)
class Validators:
    """What the server gave us last time, to quote back this time.

    Both blank is the ordinary unsupported case, and ``headers()`` then answers
    an empty dict — a plain unconditional GET, i.e. today's behaviour.
    """

    etag: str = ""
    last_modified: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.etag or self.last_modified)

    def headers(self) -> dict[str, str]:
        """``If-None-Match`` / ``If-Modified-Since``, or nothing at all.

        Both are sent when both are known. RFC 9110 §13.1.3 makes
        ``If-None-Match`` take precedence where a server honours it, and a
        server that only understands the date still gets one it understands —
        which is the point, since we do not know which of the two (if either)
        ServiceTitan and its CDN implement.
        """
        sent: dict[str, str] = {}
        if self.etag:
            sent["If-None-Match"] = self.etag
        if self.last_modified:
            sent["If-Modified-Since"] = self.last_modified
        return sent


def looks_signed(url: str) -> bool:
    """True when this url carries a credential that will differ next listing.

    Cheap and syntactic — no request is made to find out. A signed url defeats
    conditional requests twice over: the validator is for a url we will never
    ask for again, and (for an asset with no ``id``) the ledger's own
    ``asset_ref`` is built from the url, so the previous run's entry is not even
    found. Counting them is how one live run tells us whether that is this
    tenant's situation.
    """
    if not url.startswith("http"):
        return False
    query = urlsplit(url).query
    if not query:
        return False
    names = parse_qs(query, keep_blank_values=True)
    return any(name.lower() in _SIGNED_QUERY_PARAMS for name in names)


@dataclass
class ConditionalReport:
    """Counters answering "does conditional fetching work on this tenant?".

    Every field is a fact observed on the wire this run, never an inference.
    """

    #: Fetches issued carrying at least one `If-*` header.
    conditional_sent: int = 0
    #: Of those, answered 304 — the whole point: verified, zero bytes.
    not_modified: int = 0
    #: Of those, answered 200 anyway (changed bytes, or an ignored validator).
    conditional_missed: int = 0
    #: 200 responses that carried an `ETag` and/or a `Last-Modified`.
    validators_present: int = 0
    #: 200 responses that carried NEITHER. These assets can never go conditional
    #: and keep the old full-re-download schedule.
    validators_absent: int = 0
    #: Of `validators_present`, how many were a weak (`W/"…"`) ETag.
    weak_etags: int = 0
    #: Assets whose source url looks signed, counted whether fetched or not.
    signed_urls: int = 0
    #: Signed urls with no ServiceTitan `asset.id`, so the LEDGER REFERENCE
    #: itself is unstable: a new url is a new asset every run, and neither the
    #: `modifiedOn` skip nor the validator can ever hit.
    unstable_refs: int = 0

    def observe_asset(self, *, source_url: str, has_asset_id: bool) -> None:
        """Note the shape of one asset's url, before anything is fetched."""
        if not looks_signed(source_url):
            return
        self.signed_urls += 1
        if not has_asset_id:
            self.unstable_refs += 1

    def observe_response(
        self, *, conditional: bool, status_code: int, etag: str | None, last_modified: str | None
    ) -> None:
        """Note what one fetch actually answered."""
        if conditional:
            self.conditional_sent += 1
        if status_code == 304:
            self.not_modified += 1
            return
        if conditional:
            self.conditional_missed += 1
        if etag or last_modified:
            self.validators_present += 1
            if etag and _WEAK_ETAG.match(etag):
                self.weak_etags += 1
        else:
            self.validators_absent += 1

    @property
    def fetches(self) -> int:
        """Every fetch that reached a status, conditional or not."""
        return self.validators_present + self.validators_absent + self.not_modified

    def as_log_fields(self) -> str:
        """The one line to read after the first live run. Counts, never per-asset."""
        return (
            f"fetches={self.fetches} conditional_sent={self.conditional_sent} "
            f"not_modified={self.not_modified} conditional_missed={self.conditional_missed} "
            f"validators_present={self.validators_present} "
            f"validators_absent={self.validators_absent} "
            f"weak_etags={self.weak_etags} signed_urls={self.signed_urls} "
            f"unstable_refs={self.unstable_refs}"
        )

    def verdict(self) -> str:
        """One English sentence naming what the counters mean.

        The counters alone need a reader who remembers what was being tested.
        This does not: it is written to be legible in a workflow log six months
        from now by somebody who has never read this module.
        """
        if self.fetches == 0:
            return "no asset was fetched this run, so nothing was learned about validators"
        if self.validators_present == 0 and self.not_modified == 0:
            extra = ""
            if self.signed_urls:
                extra = (
                    f" {self.signed_urls} of the urls look signed, which would explain it"
                    " and cannot be worked around from this side"
                )
            return (
                "NO validator on any response: conditional requests are inert on this "
                f"tenant and every asset stays on the full re-download schedule.{extra}"
            )
        if self.conditional_sent == 0:
            return (
                "validators are being returned and stored, but no conditional request "
                "was sent yet — the next run is the one that proves 304s"
            )
        if self.not_modified == 0:
            return (
                f"{self.conditional_sent} conditional request(s) sent and NOT ONE 304: "
                "the validators are being ignored, or the urls change per request"
            )
        share = round(100 * self.not_modified / self.conditional_sent)
        return (
            f"conditional requests WORK: {self.not_modified}/{self.conditional_sent} "
            f"({share}%) answered 304 and cost no bytes"
        )

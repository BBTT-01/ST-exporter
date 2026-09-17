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

ONE DOWNLOAD PER IMAGE, ONE UPLOAD PER ITEM
===========================================

Several pricebook items routinely share one photograph. Run 35145303072 on
`BBTT-01/tr-doorservpro` fetched five assets and reported ``fetched=5``: items
2141068, 2141069, 2141070, 2141071 and 2141078 all pointed at the SAME asset
GUID, so the pass downloaded one picture five times.

The two halves of the work are keyed differently and must be treated
differently:

- the DOWNLOAD is keyed by the asset's ``source_url`` — the same url is the same
  bytes, so it is fetched **once per run** and the bytes are fanned out;
- the UPLOAD is keyed by ``external_item_id`` — TrueQuote's route takes one item
  per POST and hardcodes ``is_primary``, so a picture shared by five items still
  needs five POSTs. There is no batch endpoint to ask for.

So: fetch once, hash once, upload per item. Everything that is a property of the
BYTES (the size cap, the byte sniff, the placeholder floor) is decided once for
the group and then counted once per member, because ``considered`` counts
item-assets and has to keep reconciling.

Nothing about the LEDGER changes shape. Its ``asset_ref`` is
``{external_item_id}:{identity}`` and its ``idempotency_key`` hashes the item,
the identity, the url and the payload — both per ITEM, not per image — so five
items sharing one picture still hold five ledger rows, five keys and five
``seen_keys`` entries. That is what keeps ``ImageLedger.keep`` honest: the prune
drops every key the run did not SEE, and a member that never contributed its key
would be pruned out and re-uploaded for ever.

CONCURRENCY
===========

The pass is almost entirely network wait — a download, then a POST of the same
bytes — so it is run on a bounded pool of worker threads. What the bound is for:

- **Memory.** A payload exists only while a worker is holding it, so at most
  ``concurrency`` images (8 MiB each, ``MAX_IMAGE_BYTES``) are ever resident.
  The dispatcher is held behind a semaphore rather than being allowed to queue
  the whole catalogue, so "tasks submitted" cannot run away from "work done".
- **The deadline and the cap.** Both are still decided by the SINGLE dispatcher
  thread, in least-recently-verified order, BEFORE a group is handed to a
  worker. That is what keeps them deterministic when tasks finish out of order:
  which assets a bounded run reaches is fixed by the dispatch order, not by
  which download happened to return first.
- **TrueQuote's rate limit.** Concurrency is not a rate; ``images/pacing.py``
  is. See it for why the two are separate dials.

``stopped`` is set by the first worker that hits a connection-level failure and
read by the dispatcher, which then stops handing out work. Groups already in
flight finish — they are whole assets, and their uploads are real and must be
recorded — so a stopped pass still ends on a flushable ledger.

THE LEDGER IS A GOOGLE SHEET
============================

``ImageLedger.flush`` rewrites the whole ``_image_ledger`` grid in one call.
Under concurrency a per-asset write would be a read-modify-write of a 16,000-row
grid per image, which is not slow so much as impossible. So the ledger is
mutated in memory under the pass's single lock and flushed on a TIMER
(``LEDGER_FLUSH_SECONDS``), from the dispatcher thread, while it holds that lock
— so every flush is a consistent snapshot. The interval is the only thing a
killed process can lose, and losing it costs re-sent bytes rather than
correctness.


Neither the order nor the ledger is a queue. The catalogue is still the queue,
and the ledger still only ever says what has already been delivered: losing it
costs bandwidth, never correctness.
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from statistics import median
from threading import RLock, Semaphore
from time import monotonic
from typing import Any, Iterable

import httpx

from st_cli.client import ServiceTitanClient
from st_cli.exceptions import APIError, STCLIError, TransportError
from st_exporter.images.assets import (
    BLANK_DENSITY_BYTES_PER_PIXEL,
    MAX_IMAGE_BYTES,
    MIN_PLAUSIBLE_IMAGE_BYTES,
    PricebookAsset,
    describe_image_size,
    describe_rejected_payload,
    idempotency_key,
    is_placeholder_image,
    is_storage_path,
    looks_blank,
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
from st_exporter.images.pacing import (
    PENALTY_SECONDS,
    TRUEQUOTE_UPLOADS_PER_SECOND,
    RateLimiter,
)
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

# How many workers fetch-and-upload at once, when the caller names no number.
#
# Eight, for three reasons that all happen to agree:
#
# - MEMORY. A payload exists only while a worker holds it, and `MAX_IMAGE_BYTES`
#   is 8 MiB, so the worst case resident set is 8 x 8 MiB = 64 MiB on a runner
#   with gigabytes. Sixteen would also be fine; sixty-four would not, which is
#   why `MAX_IMAGE_CONCURRENCY` exists.
# - THROUGHPUT. The work is ~100% network wait, so speedup is close to linear in
#   the worker count until a rate limit binds. Eight turns a ~20-hour first sync
#   into a ~2.5-hour one, which is the difference between "needs four jobs" and
#   "fits in one".
# - RATE. At eight workers the pass produces under two requests a second to
#   either service, comfortably under both governors in `images/pacing.py`. The
#   default is therefore conservative in the only sense that matters: it cannot
#   be the thing that trips a limit nobody has measured.
DEFAULT_IMAGE_CONCURRENCY = 8

# The dial's ceiling. Not a guess about what is fast, a bound on what is SAFE:
# 32 x 8 MiB is 256 MiB of image bytes resident, which is the point at which a
# 7 GB runner starts to care, and 32 sockets is already twice what
# `ServiceTitanClient` will keep alive.
MAX_IMAGE_CONCURRENCY = 32

# The default ceiling, in requests per second, applied SEPARATELY to each side:
# ServiceTitan downloads and TrueQuote uploads. See `images/pacing.py` for why
# this is a real token bucket rather than the retry backoff that was here
# before, and why a ceiling that never binds at the default concurrency is
# still worth having.
DEFAULT_REQUESTS_PER_SECOND = 6.0

# How often the in-memory ledger is written back to its Sheet tab MID-PASS.
#
# The flush is a whole-grid rewrite (`ImageLedger.flush`), so it cannot be done
# per asset — under concurrency that would be a read-modify-write of a
# 16,000-row grid per image. It is done on a timer instead, from the dispatcher
# thread, holding the pass's lock so the snapshot is consistent.
#
# Sixty seconds because a killed process is the EXPECTED case for a multi-hour
# backfill, not the exception: GitHub's runner limit, a cancelled workflow, a
# spot reclaim. Sixty seconds of a pass moving ~2 images a second is ~120
# uploads re-sent by the next run — bytes, never correctness — against a flush
# cost of a few seconds every minute. Longer would be cheaper and lose more;
# shorter would spend a visible fraction of the run writing a spreadsheet.
LEDGER_FLUSH_SECONDS = 60.0

# How often a long pass says where it has got to. A multi-hour run with no
# output is indistinguishable from a hung one.
PROGRESS_EVERY_SECONDS = 30.0

# How many individual successful uploads are described in full on a CAPPED run.
#
# A capped run is a PROVING run: somebody set a cap precisely so they could go
# and look at the result. Naming only the failures — which is what this lane did
# — is backwards, so a capped run names every success, with its item id and its
# byte size. Bounded anyway, because `image_max_assets: 20000` is legal.
UPLOAD_DETAIL_LIMIT = 500

# How many REFUSED uploads get a full WARNING line naming the image.
#
# Its own cap, and deliberately NOT gated on the run being capped the way
# `UPLOAD_DETAIL_LIMIT` is: a refusal is the one outcome nobody can act on
# without knowing WHICH image it was, and run 35152460933 logged exactly
# `rejected=1` with no way to find it. Bounded all the same, because a
# TrueQuote-side change could refuse a 16,000-image catalogue wholesale and the
# log is not the place to enumerate that; past the cap the summary's
# `rejected` count carries on alone.
REJECTION_DETAIL_LIMIT = 500

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


def _human_bytes(count: int) -> str:
    """``246`` / ``3.1K`` / ``480K`` / ``1.2M``. Log-friendly, never scientific."""
    if count < 1024:
        return str(count)
    for unit, scale in (("K", 1024), ("M", 1024**2), ("G", 1024**3)):
        if count < scale * 1024 or unit == "G":
            value = count / scale
            return f"{value:.1f}{unit}" if value < 10 else f"{value:.0f}{unit}"
    return str(count)  # pragma: no cover - unreachable, G is the last branch


@dataclass
class ImageUploadSummary:
    """What one image pass did. Every counter is a distinct, named outcome."""

    considered: int = 0
    no_image: int = 0
    already_uploaded: int = 0
    uploaded: int = 0
    download_failed: int = 0
    upload_rejected: int = 0
    # Assets NOT POSTed this run because the ledger remembers TrueQuote
    # permanently refusing these exact bytes on an earlier run. Its own counter
    # rather than folded into `already_uploaded`, because the two are opposite
    # facts: one says the image is delivered, the other says it never will be
    # until the bytes change. A run whose `rejected_remembered` climbs run over
    # run is a catalogue TrueQuote will not take, not a converging sweep.
    rejected_remembered: int = 0
    unsupported: int = 0
    too_large: int = 0
    # Byte-valid images too small to be a picture: ServiceTitan answers 200 OK
    # with a blank placeholder where the caller may not see the real asset, and
    # a placeholder passes the sniff. Counted UNDER ITS OWN NAME and never
    # folded into `unsupported`, because the two ask for opposite fixes — an
    # `unsupported` run says "we are not fetching images", a `placeholder` run
    # says "we are fetching them and they are blank, check the permission".
    placeholders: int = 0
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
    # DOWNLOADS this run made — distinct images pulled over the network, 304s
    # included. NOT item-assets: several items routinely share one picture and
    # it is fetched once (see the module docstring). This is what `max_assets`
    # caps, because it is the expensive half and because a `modifiedOn` skip is
    # free and must not consume a budget that exists to bound WORK.
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

    # ---- the dedupe measurement -------------------------------------------
    # Every uploadable ITEM-ASSET in the catalogue this pass was handed, and how
    # many DISTINCT images they point at. Counted over the whole listing, free,
    # before anything is fetched — the listing is already in memory. The ratio
    # between them is the single number that says how much of this lane's work
    # was ever real: run 35145303072 fetched one GUID five times.
    item_assets: int = 0
    distinct_assets: int = 0

    # ---- the byte-size measurement ----------------------------------------
    # The size of every payload this run successfully uploaded. Nobody has
    # confirmed that what ServiceTitan serves us is a usable photograph rather
    # than a thumbnail — the 1 KiB placeholder floor's "real assets run 2-4 KiB"
    # is an ASSUMPTION, never a measurement (see `assets.MIN_PLAUSIBLE_IMAGE_BYTES`)
    # — and a 3 KB payload and a 300 KB payload are the two opposite answers.
    # Kept as the raw sizes so the summary can report a median rather than only
    # a mean, which on image sizes is the difference between a number and noise.
    upload_sizes: list[int] = field(default_factory=list)
    # How many individual uploads have already been described in full, and how
    # many mid-pass ledger flushes happened (and failed).
    upload_detail_logged: int = 0
    # How many REFUSALS have been described in full. Bounds the log, never the
    # counting.
    rejection_detail_logged: int = 0
    # Uploads whose compressed BYTES PER PIXEL say the picture is a flat fill.
    #
    # Counted, reported, and NEVER acted on. The 1 KiB byte floor cannot catch a
    # blank: ServiceTitan's web-app proxy was measured on 2026-09-16 serving a
    # completely white 1200x1200 WebP in 2,798 bytes, which clears the floor,
    # and its size scales with the requested `size=` so no constant can work.
    # Density does scale — see `assets.BLANK_DENSITY_BYTES_PER_PIXEL` — but no
    # real pricebook asset from this tenant has EVER been measured, so there is
    # nothing to set a rejection threshold against. This counter is how that
    # gets measured: a run reporting `images_suspected_blank=16000` is the
    # finding, and a rule can then be written from evidence instead of guessed.
    suspected_blank: int = 0
    ledger_flushes: int = 0
    ledger_flush_failures: int = 0
    # How long workers spent held back by the rate governors, and how many 429s
    # the governors were told about. Both zero on a healthy run.
    throttled_seconds: float = 0.0
    rate_limit_penalties: int = 0

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
    def dedupe_ratio(self) -> float:
        """Item-assets per distinct image. 1.0 means nothing is shared."""
        if not self.distinct_assets:
            return 1.0
        return self.item_assets / self.distinct_assets

    @property
    def downloads_saved(self) -> int:
        """Fetches the whole catalogue avoids by sharing, at one run per image."""
        return max(0, self.item_assets - self.distinct_assets)

    @property
    def bytes_field(self) -> str:
        """``min/median/max/total`` over the payloads actually uploaded.

        The answer to "are these photographs or thumbnails", on the run's own
        summary line where nobody has to go looking for it.
        """
        if not self.upload_sizes:
            return "min=- median=- max=- total=0"
        return (
            f"min={_human_bytes(min(self.upload_sizes))} "
            f"median={_human_bytes(int(median(self.upload_sizes)))} "
            f"max={_human_bytes(max(self.upload_sizes))} "
            f"total={_human_bytes(sum(self.upload_sizes))}"
        )

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
            f"rejected_remembered={self.rejected_remembered} "
            f"unsupported={self.unsupported} "
            f"unsupported_shapes={self.unsupported_shapes_field} "
            f"too_large={self.too_large} placeholders={self.placeholders} "
            f"revalidated={self.revalidated} not_modified={self.not_modified} "
            f"fetched={self.fetched} "
            f"item_assets={self.item_assets} distinct_assets={self.distinct_assets} "
            f"dedupe={self.dedupe_ratio:.2f}x "
            f"bytes[{self.bytes_field}] "
            f"suspected_blank={self.suspected_blank} "
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
    concurrency: int | None = None,
    requests_per_second: float | None = None,
    flush_interval: float | None = None,
) -> ImageUploadSummary:
    """Upload one image per pricebook item. Never raises for a single bad asset.

    ``deadline`` is a ``time.monotonic()`` reading past which no further asset is
    started. None means no budget, which is only safe where nothing will kill the
    process — in tests, and in a local run.

    ``max_assets`` caps how many DOWNLOADS this run makes — how many distinct
    images reach the network at all. ``NO_ASSET_CAP`` (0, the default) means no
    cap. Two stopping conditions, not one, because they bound different things:
    the deadline bounds the CLOCK (and the thing it protects is the ledger
    flush), the cap bounds the WORK (and the thing it protects is a tenant's
    rate limit and bandwidth while a strategy settles). They set different
    ``stopped`` reasons and both flush, because both leave the pass at the end
    of a whole asset.

    A `modifiedOn` skip never consumes the cap. It costs no request, and a cap
    that counted it would stop a converged catalogue part-way through a sweep it
    could have finished for free — and, worse, would make every run report
    ``stopped`` and so never prune the ledger.

    ``concurrency`` is how many assets are fetched-and-uploaded at once
    (``DEFAULT_IMAGE_CONCURRENCY``, ``EXPORTER_IMAGE_CONCURRENCY``).
    ``requests_per_second`` is the shared ceiling each service is held to
    (``DEFAULT_REQUESTS_PER_SECOND``, ``EXPORTER_IMAGE_REQUESTS_PER_SECOND``);
    the TrueQuote side is additionally clamped to their documented limit. Both
    exist for the same reason and neither replaces the other — see
    ``images/pacing.py``.

    ``flush_interval`` is how often the ledger is written back mid-pass, in
    seconds of real time (``LEDGER_FLUSH_SECONDS``). It is the maximum amount of
    delivered-upload knowledge a killed process can lose. 0 flushes at every
    opportunity; a negative number never flushes mid-pass.
    """
    summary = ImageUploadSummary(max_assets=max_assets)
    workers = _bounded_concurrency(concurrency)
    pace = DEFAULT_REQUESTS_PER_SECOND if requests_per_second is None else requests_per_second
    owns_http = http is None
    public = http or httpx.Client(
        timeout=_PUBLIC_TIMEOUT,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=max(workers * 2, 10)),
    )
    pass_ = _ImagePass(
        client=client,
        image_client=image_client,
        ledger=ledger,
        public=public,
        summary=summary,
        now=now,
        workers=workers,
        # The same ceiling on each side, because the number means "how hard this
        # pass is allowed to push ONE service", and it pushes two. TrueQuote's
        # is clamped to what they published: a configuration mistake may slow
        # the pass down, never take it past a documented limit.
        download_limiter=RateLimiter(pace),
        upload_limiter=RateLimiter(min(pace, TRUEQUOTE_UPLOADS_PER_SECOND)),
        flush_interval=LEDGER_FLUSH_SECONDS if flush_interval is None else flush_interval,
    )
    try:
        pass_.run(records, deadline=deadline, max_assets=max_assets)
    finally:
        if owns_http:
            public.close()
    return summary


def _bounded_concurrency(concurrency: int | None) -> int:
    """The worker count, clamped into ``[1, MAX_IMAGE_CONCURRENCY]``.

    Clamped rather than rejected: this is a performance dial on a side lane, and
    a nonsensical value must slow a tenant's images down, never fail their run.
    """
    if concurrency is None:
        return DEFAULT_IMAGE_CONCURRENCY
    return max(1, min(MAX_IMAGE_CONCURRENCY, concurrency))


@dataclass(frozen=True)
class _DownloadGroup:
    """Every item-asset that would download the SAME bytes. Fetched once.

    ``members`` is in least-recently-verified order and never empty; the first
    is the one whose url and ``asset_id`` describe the fetch. The upload is a
    separate matter — TrueQuote's route is keyed by ``external_item_id``, so
    every member gets its own POST of the shared bytes.
    """

    source_url: str
    members: tuple[PricebookAsset, ...]
    # The validators to quote back, and ONLY when every member agrees on them.
    # Disagreement means at least one member is in a different state from the
    # others — most importantly, one that has never been delivered — and a 304
    # would then verify an asset TrueQuote has never received. Falling back to
    # an unconditional GET is exactly what a single-member group with no stored
    # validator already does, so the shared case is never weaker than the
    # unshared one.
    validators: Validators

    @property
    def lead(self) -> PricebookAsset:
        return self.members[0]


class _ImagePass:
    """One bounded, ordered, resumable pass. The dispatcher owns the decisions.

    Split into a class only because a concurrent pass has state — a lock, a
    semaphore, two governors, a stop flag — and threading it through free
    functions would be worse. The DECISIONS have not moved: the deadline, the
    cap and the order are still evaluated by one thread, in one loop, exactly
    where they were.
    """

    def __init__(
        self,
        *,
        client: ServiceTitanClient,
        image_client: TrueQuoteImageClient,
        ledger: ImageLedger,
        public: httpx.Client,
        summary: ImageUploadSummary,
        now: str,
        workers: int,
        download_limiter: RateLimiter,
        upload_limiter: RateLimiter,
        flush_interval: float,
    ) -> None:
        self._client = client
        self._image_client = image_client
        self._ledger = ledger
        self._public = public
        self._summary = summary
        self._now = now
        self._workers = workers
        self._download_limiter = download_limiter
        self._upload_limiter = upload_limiter
        self._flush_interval = flush_interval
        # ONE lock over the summary AND the ledger, because every interesting
        # mutation touches both and two locks would be two lock orders. It is
        # held only for in-memory bookkeeping — never across a request — except
        # deliberately during a flush, which must be a consistent snapshot.
        self._lock = RLock()
        # Bounds SUBMITTED work, not just running work. Without it the
        # dispatcher would queue the whole catalogue in milliseconds and the
        # deadline would never bite; with it the dispatcher advances at the pace
        # of completion, which is what keeps `stopped` and `pending` honest.
        # Twice the worker count so a finishing worker always has something to
        # pick up, and payloads still only exist inside a RUNNING task, so the
        # resident-bytes bound is `workers`, not `2 * workers`.
        self._slots = Semaphore(workers * 2)
        self._dirty = False
        # Set ONLY by a worker that hit a connection-level failure — TrueQuote
        # unreachable or rate limiting, ServiceTitan's images endpoint
        # unreachable. It means "the receiver is not accepting work", so a
        # worker still holding bytes stops pushing them.
        #
        # Deliberately NOT set by the dispatcher's own cap and deadline. Those
        # say "start nothing more"; they must not abandon an asset already in
        # flight, whose download is paid for and whose upload is the thing the
        # run exists to do. A cap of 2 has to deliver 2, whether or not the
        # second one finished before the third was refused a slot.
        self._abort = False
        self._last_flush = time.monotonic()
        self._last_progress = self._last_flush
        self._started = self._last_flush

    # -- the dispatcher -----------------------------------------------------

    def run(
        self,
        records: Iterable[dict[str, Any]],
        *,
        deadline: float | None,
        max_assets: int,
    ) -> None:
        summary = self._summary
        assets = _ordered_assets(records, self._ledger, summary)
        freshness = [_is_still_fresh(asset, self._ledger, now=self._now) for asset in assets]
        groups = _download_groups(assets, freshness, self._ledger)
        self._log_plan()

        # Observe ServiceTitan's own 429 backoff so it slows every worker down
        # rather than each one independently. Restored afterwards: the client
        # outlives this pass and belongs to the run, not to us.
        previous_observer = self._client.on_rate_limited
        self._client.on_rate_limited = self._on_servicetitan_throttled

        dispatched: set[str] = set()
        attempted = 0
        futures: set[Future[None]] = set()
        try:
            with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="image") as pool:
                for asset, fresh in zip(assets, freshness):
                    if not fresh and asset.source_url in dispatched:
                        # Its picture already went out with an earlier member of
                        # the same group; it was counted then. Not new work, so
                        # it costs neither a clock reading nor a cap slot.
                        continue
                    if self._stop_reason() is not None:
                        break
                    reading = monotonic()
                    if deadline is not None and reading >= deadline:
                        self._stop(BUDGET_SPENT)
                        break
                    if max_assets != NO_ASSET_CAP and self._fetched() >= max_assets:
                        self._stop(ASSET_CAP_REACHED)
                        break

                    if fresh:
                        attempted += 1
                        self._revalidate(asset)
                        self._maintain(attempted, len(assets))
                        continue

                    group = groups[asset.source_url]
                    dispatched.add(group.source_url)
                    attempted += len(group.members)
                    if not self._admit(group):
                        continue
                    self._slots.acquire()
                    futures.add(pool.submit(self._process_group, group))
                    futures = {f for f in futures if not f.done()}
                    self._maintain(attempted, len(assets))
                # Every group already handed out is a WHOLE asset whose upload
                # is real and must be recorded, so a stopped pass drains rather
                # than abandons. There is nothing to wait for that is not
                # already bounded by the HTTP timeouts.
                wait(futures)
        finally:
            self._client.on_rate_limited = previous_observer

        summary.pending = len(assets) - attempted
        self._flush_ledger(force=True)
        self._log_progress(attempted, len(assets), force=True)
        self._log_outcome(max_assets)

    def _admit(self, group: _DownloadGroup) -> bool:
        """Count the group in, and say whether it may reach the network.

        The single place `considered` and `fetched` move, and it is the
        DISPATCHER thread that moves them — which is what makes the cap
        deterministic when downloads finish out of order.
        """
        with self._lock:
            self._summary.considered += len(group.members)
            if is_storage_path(group.source_url) and self._summary.permission_denied:
                # Already told, once, that this tenant will not serve image
                # bytes. Not a fetch, so not a cap slot either.
                return False
            self._summary.fetched += 1
        return True

    # -- the workers --------------------------------------------------------

    def _process_group(self, group: _DownloadGroup) -> None:
        """One download, fanned out to every item that shares it."""
        try:
            summary = self._summary
            with self._lock:
                summary.conditional.observe_asset(
                    source_url=group.source_url, has_asset_id=bool(group.lead.asset_id)
                )
            fetched = self._fetch(group)
            if fetched is None:
                # `_fetch` sets `stopped` when the failure is about the
                # CONNECTION to ServiceTitan rather than about this one asset;
                # the dispatcher reads that and stops handing out work.
                return
            if fetched.payload is None:
                # 304 NOT MODIFIED for every member at once. The server checked
                # our validator against the live bytes and said "nothing new".
                # No bytes, no content hash, no upload; just a fresh
                # verification per member, keyed on the ASSET reference rather
                # than on a content hash we did not compute.
                with self._lock:
                    for member in group.members:
                        ref = asset_ref(member)
                        summary.not_modified += 1
                        summary.already_uploaded += 1
                        summary.seen_keys |= self._ledger.keys_for(ref)
                        self._ledger.verify_ref(ref, self._now)
                    self._dirty = True
                return

            payload = fetched.payload
            # Everything below the fetch is a property of the BYTES, so it is
            # decided ONCE for the group and then counted once per member —
            # `considered` counts item-assets and has to keep reconciling.
            if len(payload) > MAX_IMAGE_BYTES:
                with self._lock:
                    summary.too_large += len(group.members)
                logger.info(
                    "pricebook image over the %d byte cap; skipped for %d item(s)",
                    MAX_IMAGE_BYTES,
                    len(group.members),
                )
                return

            content_type = sniff_content_type(payload)
            if content_type is None:
                # TrueQuote would 422 these (`image_content_mismatch`); no point
                # sending. What ARRIVED is recorded, because "unsupported" alone
                # cannot tell an HTML login page from a GIF and those ask for
                # opposite fixes.
                with self._lock:
                    _record_unsupported(summary, group, payload, fetched)
                return

            if is_placeholder_image(payload):
                # A well-formed image that is too small to BE a picture. It
                # would sniff clean, upload clean and leave the contractor's
                # catalogue full of blank grey squares while the run reported
                # success. Refused, under its own counter. Nothing is written to
                # the ledger: the next run asks again, which is what we want if
                # the tenant later grants the permission that makes the real
                # bytes visible.
                with self._lock:
                    _record_placeholder(summary, group, payload, fetched)
                return

            for member in group.members:
                if self._aborted():
                    # Not "the pass has stopped" — the DISPATCHER's cap and
                    # deadline stop it too, and those must not abandon bytes
                    # already paid for. Only a receiver that is refusing work.
                    return
                self._deliver(member, payload, content_type, fetched)
        except Exception as exc:  # noqa: BLE001 - one asset may not kill the pool
            # A worker that raises would otherwise lose its exception into a
            # Future nobody inspects, and the pass would quietly under-count.
            #
            # An UNPREDICTED exception is not a handled asset failure: it means
            # this code is wrong, so the pass stops (starts nothing further) and
            # says so under the same name the run wrapper has always used. It is
            # a soft stop, not an abort — whatever is already in flight has real
            # uploads to record, and the ledger flush is the only reason the
            # first upload of a dying pass is not lost.
            #
            # The members are also counted as failures, which is what keeps
            # `complete` False and so vetoes the ledger prune: assets this run
            # never delivered must not be mistaken for assets that are gone.
            with self._lock:
                self._summary.download_failed += len(group.members)
            self._stop(f"image pass aborted: {type(exc).__name__}: {exc}")
            logger.exception("pricebook image group failed unexpectedly (%s)", exc)
        finally:
            self._slots.release()

    def _deliver(
        self,
        member: PricebookAsset,
        payload: bytes,
        content_type: str,
        fetched: _Fetched,
    ) -> None:
        """One POST for one item, from bytes somebody else may have downloaded."""
        summary = self._summary
        key = idempotency_key(member, payload)
        with self._lock:
            # Into `seen_keys` BEFORE anything can go wrong with the upload: the
            # key describes bytes this run has SEEN, and `ImageLedger.keep`
            # prunes everything it did not see. A key left out here is an
            # upload forgotten and re-sent for ever.
            summary.seen_keys.add(key)
            # Two reasons not to POST, counted apart because they mean opposite
            # things: the bytes are already delivered, or TrueQuote refused
            # these exact bytes permanently on an earlier run and will refuse
            # them again. The key hashes the payload, so a REPLACED image gets a
            # new key and is offered afresh either way.
            remembered_rejection = self._ledger.is_rejected(key)
            already = self._ledger.has(key) and not remembered_rejection
            settled = already or remembered_rejection
            if already:
                summary.already_uploaded += 1
            elif remembered_rejection:
                summary.rejected_remembered += 1
            if settled:
                # Re-stamp it so it sorts to the BACK of the next pass, and
                # store the validators on the way past: this is the run that
                # teaches the ledger what to quote back, so the NEXT weekly
                # re-verification of this asset can be a 304, not a download.
                self._ledger.verify(
                    key, self._now, etag=fetched.etag, last_modified=fetched.last_modified
                )
                self._dirty = True
        if settled:
            return

        if len(member.identity) > _MAX_TEXT_FIELD:
            with self._lock:
                summary.unsupported += 1
                _count_shape(summary, "identity_too_long")
            return

        waited = self._upload_limiter.acquire()
        result = self._image_client.upload(
            external_item_id=member.external_item_id,
            source_url=member.source_url,
            content_type=content_type,
            payload=payload,
            idempotency_key=key,
            asset_id=member.asset_id,
            filename=_capped(member.filename),
            alias=_capped(member.alias),
            asset_type=_capped(member.asset_type),
            is_default=member.is_default,
        )
        with self._lock:
            summary.throttled_seconds += waited

        if isinstance(result, ImageUploadAccepted):
            with self._lock:
                summary.uploaded += 1
                summary.upload_sizes.append(len(payload))
                if looks_blank(payload):
                    summary.suspected_blank += 1
                self._ledger.record(
                    ImageLedgerEntry(
                        idempotency_key=key,
                        asset_ref=asset_ref(member),
                        storage_path=result.storage_path,
                        verified_at=self._now,
                        etag=fetched.etag,
                        last_modified=fetched.last_modified,
                    )
                )
                self._dirty = True
                self._log_upload(member, payload, content_type)
            return

        assert isinstance(result, ImageUploadRejected)
        if result.retryable:
            if result.status_code == 429:
                # Tell the governor before giving up, so anything still in
                # flight stops pushing at the rate that earned this.
                self._upload_limiter.penalise()
                with self._lock:
                    summary.rate_limit_penalties += 1
            self._stop_hard(result.error)
            logger.warning(
                "TrueQuote image upload stopped after HTTP %d (%s); "
                "the remaining images are retried next run",
                result.status_code,
                result.error,
            )
            return

        with self._lock:
            summary.upload_rejected += 1
            # A permanent refusal is remembered, keyed on the SAME idempotency
            # key the accepted path writes. Without this row the next run
            # re-downloads these bytes, re-POSTs them, and is refused again --
            # for ever, on every run, with nothing in the log naming the image.
            self._ledger.record_rejected(
                ImageLedgerEntry(
                    idempotency_key=key,
                    asset_ref=asset_ref(member),
                    storage_path="",
                    verified_at=self._now,
                    etag=fetched.etag,
                    last_modified=fetched.last_modified,
                )
            )
            self._dirty = True
            self._log_rejection(member, payload, content_type, result)

    def _fetch(self, group: _DownloadGroup) -> _Fetched | None:
        """One image, fetched CONDITIONALLY where we have something to quote back.

        Returns None when it could not be fetched at all (already counted).

        ``group.validators`` carries the ``ETag``/``Last-Modified`` the ledger
        kept, and only when every member agrees on them. When it is empty — a
        first fetch, a pre-upgrade ledger row, a server that offers no
        validators, or members in different states — the headers are empty too
        and this is precisely the unconditional GET it has always been. That is
        the fallback, and it is the default: nothing here assumes ServiceTitan
        implements conditional requests, and the one thing the pass insists on
        is COUNTING what came back (``summary.conditional``).
        """
        summary = self._summary
        asset = group.lead
        conditional_headers = group.validators.headers()
        waited = self._download_limiter.acquire()
        with self._lock:
            summary.throttled_seconds += waited
        try:
            if is_storage_path(asset.source_url):
                file = self._client.get_file(
                    _IMAGES_MODULE,
                    _IMAGES_RESOURCE,
                    params={"path": asset.source_url},
                    headers=conditional_headers or None,
                )
                status, body = file.status_code, file.content
                etag, last_modified = file.etag, file.last_modified
                declared = file.content_type
            else:
                resp = self._public.get(asset.source_url, headers=conditional_headers)
                # 304 is a success, and `raise_for_status` agrees (it raises
                # only on 4xx/5xx) — but say so explicitly, because a future
                # httpx that treated 3xx as an error here would silently turn
                # every cheap verification into a `download_failed`.
                if resp.status_code == 429:
                    self._public_throttled(resp)
                if resp.status_code != 304:
                    resp.raise_for_status()
                status, body = resp.status_code, resp.content
                etag = resp.headers.get("etag")
                last_modified = resp.headers.get("last-modified")
                declared = resp.headers.get("content-type")
            with self._lock:
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
                with self._lock:
                    if not summary.permission_denied:
                        summary.permission_denied = True
                        logger.warning(
                            "ServiceTitan refused the pricebook images endpoint (403). The "
                            "tenant has not granted `Pricebook -> Images`; pricebook image "
                            "upload is skipped this run. Every other tab is unaffected."
                        )
            else:
                if exc.status_code == 429:
                    self._on_servicetitan_throttled(0.0)
                with self._lock:
                    summary.download_failed += len(group.members)
                logger.info("pricebook image download failed: %s", exc)
            return None
        except TransportError as exc:
            with self._lock:
                summary.download_failed += len(group.members)
            if is_storage_path(asset.source_url):
                # ServiceTitan itself is unreachable, not this one image. The
                # same failure awaits every other authenticated asset, and each
                # one now costs a full retry budget of timeouts — five of them
                # exceed the workflow's timeout-minutes on their own. Stop the
                # PASS, the way a 403 already does; the next scheduled run
                # retries every asset.
                self._stop_hard(f"ServiceTitan images endpoint unreachable: {exc}")
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
            with self._lock:
                summary.download_failed += len(group.members)
            logger.info("pricebook image download failed: %s", exc)
            return None

    # -- shared state -------------------------------------------------------

    def _revalidate(self, asset: PricebookAsset) -> None:
        """The download that never happens.

        The asset's existing keys go into `seen_keys` by hand because no content
        hash was computed this run, and a key absent from `seen_keys` is what
        `ImageLedger.keep` prunes — without this, a converged catalogue would
        prune itself empty and re-upload everything on the run after that.
        """
        with self._lock:
            self._summary.considered += 1
            self._summary.revalidated += 1
            self._summary.already_uploaded += 1
            self._summary.seen_keys |= self._ledger.keys_for(asset_ref(asset))

    def _stop(self, reason: str) -> None:
        """End the pass: start nothing new. First reason wins."""
        with self._lock:
            if self._summary.stopped is None:
                self._summary.stopped = reason

    def _stop_hard(self, reason: str) -> None:
        """End the pass AND abandon work in flight. The receiver is down."""
        with self._lock:
            if self._summary.stopped is None:
                self._summary.stopped = reason
            self._abort = True

    def _aborted(self) -> bool:
        with self._lock:
            return self._abort

    def _stop_reason(self) -> str | None:
        with self._lock:
            return self._summary.stopped

    def _fetched(self) -> int:
        with self._lock:
            return self._summary.fetched

    def _on_servicetitan_throttled(self, wait: float) -> None:
        """ServiceTitan said 429 to somebody. Hold the whole pass back.

        The retry itself is still `st_cli.client`'s, unchanged. What this adds
        is that the OTHER workers stop pushing at the rate that earned it,
        instead of each discovering the same 429 on their own clock and waking
        up together. The penalty is at least as long as the backoff the client
        is about to take, so the governor never lets a worker through earlier
        than the retry it is waiting for.
        """
        self._download_limiter.penalise(max(wait, PENALTY_SECONDS))
        with self._lock:
            self._summary.rate_limit_penalties += 1

    def _public_throttled(self, resp: httpx.Response) -> None:
        """A 429 from a public CDN. Same governor, no retry of our own."""
        retry_after = resp.headers.get("retry-after", "")
        try:
            seconds = float(retry_after)
        except ValueError:
            seconds = 5.0
        self._download_limiter.penalise(seconds)
        with self._lock:
            self._summary.rate_limit_penalties += 1

    # -- housekeeping -------------------------------------------------------

    def _maintain(self, attempted: int, total: int) -> None:
        """Flush the ledger and report progress, both on their own timers.

        Called from the DISPATCHER only, and the flush takes the pass's lock, so
        every written grid is a consistent snapshot of a moment no worker was
        half-way through recording.

        Deliberately on ``time.monotonic`` rather than on the pass's ``monotonic``
        name: that one is the deadline clock, which tests replace with a counter
        advancing one reading per asset. Housekeeping must not consume readings
        from it, or adding a progress line would move where a budget bites.
        """
        self._flush_ledger()
        self._log_progress(attempted, total)

    def _flush_ledger(self, *, force: bool = False) -> None:
        if self._flush_interval < 0 and not force:
            return
        now = time.monotonic()
        if not force and now - self._last_flush < self._flush_interval:
            return
        with self._lock:
            if not self._dirty:
                self._last_flush = now
                return
            try:
                self._ledger.flush()
            except Exception as exc:  # noqa: BLE001 - a side lane may not end the run
                self._summary.ledger_flush_failures += 1
                logger.warning(
                    "mid-pass image ledger flush failed (%s); the pass continues and the "
                    "final flush is still attempted. Uploads made since the last successful "
                    "flush are re-sent by the next run if this process is killed.",
                    exc,
                )
                self._last_flush = now
                return
            self._summary.ledger_flushes += 1
            self._dirty = False
        self._last_flush = now

    def _log_plan(self) -> None:
        """The dedupe measurement, stated before a single byte is fetched."""
        summary = self._summary
        logger.info(
            "image asset sharing: %d uploadable item-asset(s) reference %d distinct "
            "image(s) (%.2fx) -- %d download(s) this pass does not have to make. "
            "%d item(s) have no image at all.",
            summary.item_assets,
            summary.distinct_assets,
            summary.dedupe_ratio,
            summary.downloads_saved,
            summary.no_image,
        )
        logger.info(
            "image pass concurrency=%d downloads<=%.2f/s uploads<=%.2f/s ledger flush every %.0fs",
            self._workers,
            self._download_limiter.per_second,
            self._upload_limiter.per_second,
            self._flush_interval,
        )

    def _log_progress(self, attempted: int, total: int, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress < PROGRESS_EVERY_SECONDS:
            return
        self._last_progress = now
        elapsed = max(now - self._started, 1e-6)
        rate = attempted / elapsed
        remaining = max(total - attempted, 0)
        eta = f"{remaining / rate / 60:.0f}m" if rate > 0 and remaining else "-"
        with self._lock:
            summary = self._summary
            logger.info(
                "image pass progress: %d/%d item-assets (%.1f%%) uploaded=%d fetched=%d "
                "revalidated=%d already=%d failed=%d rate=%.2f/s elapsed=%.0fs eta=%s",
                attempted,
                total,
                100.0 * attempted / total if total else 100.0,
                summary.uploaded,
                summary.fetched,
                summary.revalidated,
                summary.already_uploaded,
                summary.download_failed + summary.upload_rejected,
                rate,
                elapsed,
                eta,
            )

    def _log_upload(self, member: PricebookAsset, payload: bytes, content_type: str) -> None:
        """Name a SUCCESS, not only a failure. Caller holds the lock.

        Only on a capped run, because a cap is somebody saying "bound this so I
        can go and look at what it did", and a proving run that names only its
        failures answers the wrong question. The byte size is on the line
        because nothing else in this system has ever reported one: it is how
        "are these photographs or 3 KB thumbnails" gets answered.
        """
        summary = self._summary
        if summary.max_assets == NO_ASSET_CAP:
            return
        if summary.upload_detail_logged >= UPLOAD_DETAIL_LIMIT:
            return
        summary.upload_detail_logged += 1
        logger.info(
            "pricebook image UPLOADED: item=%s asset=%s %s (%s) content_type=%s",
            member.external_item_id,
            redact_source_url(member.identity),
            describe_image_size(payload),
            _human_bytes(len(payload)),
            content_type,
        )
        if summary.upload_detail_logged == UPLOAD_DETAIL_LIMIT:
            logger.info(
                "further successful uploads will not be listed individually (cap %d per "
                "run); the summary's byte statistics still cover every one of them.",
                UPLOAD_DETAIL_LIMIT,
            )

    def _log_rejection(
        self,
        member: PricebookAsset,
        payload: bytes,
        content_type: str,
        result: ImageUploadRejected,
    ) -> None:
        """Name a REFUSED image. Caller holds the lock.

        Unconditional on the cap, unlike ``_log_upload``: a refusal that names
        no item, no asset, no size and no shape is unactionable, and that is
        exactly what run 35152460933 produced -- `rejected=1` and a bare
        `HTTP 413 http_413`. Same bounded, sanitised rendering as everywhere
        else in this module: the identity loses any query string and userinfo,
        and no body ever reaches the log.
        """
        summary = self._summary
        if summary.rejection_detail_logged >= REJECTION_DETAIL_LIMIT:
            return
        summary.rejection_detail_logged += 1
        logger.warning(
            "TrueQuote REFUSED one pricebook image (HTTP %d %s); the run continues: "
            "item=%s asset=%s source=%s %s (%s) content_type=%s. These bytes are "
            "remembered as refused and will not be sent again until the image changes.",
            result.status_code,
            result.error,
            member.external_item_id,
            redact_source_url(member.identity),
            redact_source_url(member.source_url),
            describe_image_size(payload),
            _human_bytes(len(payload)),
            content_type,
        )
        if summary.rejection_detail_logged == REJECTION_DETAIL_LIMIT:
            logger.warning(
                "further refused pricebook images will not be described individually "
                "(cap %d per run); the run summary's `rejected` keeps counting every "
                "one of them.",
                REJECTION_DETAIL_LIMIT,
            )

    def _log_outcome(self, max_assets: int) -> None:
        summary = self._summary
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
        if summary.upload_sizes:
            logger.info(
                "uploaded image sizes: %s over %d upload(s). A median in the low single-digit "
                "KB would mean ServiceTitan is serving THUMBNAILS, not photographs.",
                summary.bytes_field,
                len(summary.upload_sizes),
            )
        if summary.suspected_blank:
            logger.warning(
                "%d of %d uploaded image(s) have a byte density below %.4f/pixel, which is what "
                "a BLANK fill looks like — a white 1200x1200 placeholder measured 0.0019/px and "
                "2798 bytes, clearing the %d byte floor. They were uploaded anyway: no real "
                "pricebook asset has ever been measured, so there is nothing to set a rejection "
                "threshold against yet. THIS COUNT IS THAT MEASUREMENT.",
                summary.suspected_blank,
                len(summary.upload_sizes),
                BLANK_DENSITY_BYTES_PER_PIXEL,
                MIN_PLAUSIBLE_IMAGE_BYTES,
            )
        if summary.rate_limit_penalties:
            logger.warning(
                "image pass was rate limited %d time(s) and held back %.1fs in total; "
                "lower EXPORTER_IMAGE_REQUESTS_PER_SECOND or EXPORTER_IMAGE_CONCURRENCY.",
                summary.rate_limit_penalties,
                summary.throttled_seconds,
            )
        logger.info(
            "image conditional requests: %s -- %s",
            summary.conditional.as_log_fields(),
            summary.conditional.verdict(),
        )


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

    An item with no usable image is counted as ``no_image`` and dropped HERE,
    before any network call — ``select_uploadable_asset`` answering None is the
    whole filter, and 4,148 of one tenant's 20,265 items never reach the fetch
    list because of it.
    """
    assets: list[PricebookAsset] = []
    for record in records:
        asset = select_uploadable_asset(record)
        if asset is None:
            summary.no_image += 1
            continue
        assets.append(asset)
    assets.sort(key=lambda asset: ledger.last_verified(asset_ref(asset)) or "")
    summary.item_assets = len(assets)
    summary.distinct_assets = len({asset.source_url for asset in assets})
    return assets


def _download_groups(
    assets: list[PricebookAsset],
    freshness: list[bool],
    ledger: ImageLedger,
) -> dict[str, _DownloadGroup]:
    """The assets that still need bytes, grouped by the url that supplies them.

    Insertion order is the catalogue's least-recently-verified order, and so is
    each group's member order, so dispatching a group when its FIRST member
    comes up preserves exactly the schedule a serial pass had.
    """
    members: dict[str, list[PricebookAsset]] = {}
    for asset, fresh in zip(assets, freshness):
        if fresh:
            continue
        members.setdefault(asset.source_url, []).append(asset)
    return {
        url: _DownloadGroup(
            source_url=url,
            members=tuple(group),
            validators=_shared_validators(ledger, group),
        )
        for url, group in members.items()
    }


def _shared_validators(ledger: ImageLedger, members: list[PricebookAsset]) -> Validators:
    """The validators to quote back for a shared image, or none at all.

    Conditional only when every member agrees. One member holding a different
    ``ETag`` — or, far more importantly, holding none because it has never been
    delivered — means a 304 would verify an asset TrueQuote does not have. The
    fallback is an unconditional GET, which is what an undelivered asset already
    does today, so sharing is never weaker than not sharing.
    """
    stored = {ledger.validators_for(asset_ref(member)) for member in members}
    if len(stored) != 1:
        return Validators()
    etag, last_modified = stored.pop()
    return Validators(etag, last_modified)


def asset_ref(asset: PricebookAsset) -> str:
    """The ledger's name for this asset. Known without downloading anything.

    Per ITEM, not per image: two items sharing one picture hold two refs, two
    ledger rows and two idempotency keys. That is deliberate and load-bearing —
    TrueQuote's route is keyed by ``external_item_id``, so "delivered" is a fact
    about an item, and the prune (``ImageLedger.keep``) has to be able to see
    each of them separately.
    """
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


def _count_shape(summary: ImageUploadSummary, shape: str) -> None:
    """Caller holds the pass's lock."""
    summary.unsupported_shapes[shape] = summary.unsupported_shapes.get(shape, 0) + 1


def _record_unsupported(
    summary: ImageUploadSummary,
    group: _DownloadGroup,
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
    asset = group.lead
    shape = payload_shape(payload)
    for _ in group.members:
        summary.unsupported += 1
        _count_shape(summary, shape)
    if summary.unsupported_detail_logged >= UNSUPPORTED_DETAIL_LIMIT:
        return
    summary.unsupported_detail_logged += 1
    logger.warning(
        "pricebook image REJECTED by the byte sniff (not PNG/JPEG/WEBP): "
        "item=%s asset=%s source=%s http_status=%s declared_content_type=%s %s "
        "shared_by=%d item(s)",
        asset.external_item_id,
        redact_source_url(asset.identity),
        redact_source_url(asset.source_url),
        fetched.status_code or "?",
        _capped_declared(fetched.content_type),
        describe_rejected_payload(payload),
        len(group.members),
    )
    if summary.unsupported_detail_logged == UNSUPPORTED_DETAIL_LIMIT:
        logger.warning(
            "further rejected pricebook images will not be described individually "
            "(cap %d per run); the run summary's `unsupported_shapes` keeps counting "
            "every one of them.",
            UNSUPPORTED_DETAIL_LIMIT,
        )


def _record_placeholder(
    summary: ImageUploadSummary,
    group: _DownloadGroup,
    payload: bytes,
    fetched: _Fetched,
) -> None:
    """Count a blank placeholder, and log enough to recognise it again.

    The sha256 is logged deliberately: it is the one fact that would let a
    future, narrower rule name a specific placeholder by content instead of
    trusting the size floor. Same bounded, sanitised rendering as
    ``_record_unsupported`` — a placeholder is still a body from the wire.
    """
    asset = group.lead
    summary.placeholders += len(group.members)
    if summary.unsupported_detail_logged >= UNSUPPORTED_DETAIL_LIMIT:
        return
    summary.unsupported_detail_logged += 1
    logger.warning(
        "pricebook image REJECTED as a blank placeholder (%d bytes, under the %d byte "
        "floor): item=%s asset=%s source=%s http_status=%s declared_content_type=%s "
        "sha256=%s. ServiceTitan serves a placeholder with 200 OK where the caller may "
        "not see the real asset; nothing was uploaded for this item.",
        len(payload),
        MIN_PLAUSIBLE_IMAGE_BYTES,
        asset.external_item_id,
        redact_source_url(asset.identity),
        redact_source_url(asset.source_url),
        fetched.status_code or "?",
        _capped_declared(fetched.content_type),
        sha256(payload).hexdigest(),
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

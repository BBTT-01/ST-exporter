"""Orchestrates one export run: fetch deltas, merge, denormalise, window, write.

See ``denormalize.py`` and ``meta.py`` for why the fetch is incremental (cursor per
feed) while the write is a full, freshly re-derived replace every run — the window
predicate is time-relative, so a row's membership must be re-decided every run
regardless of whether its underlying record changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable, TypeVar

from st_cli.client import ServiceTitanClient
from st_cli.config import Settings
from st_cli.exceptions import APIError, ConfigError, STCLIError
from st_exporter import EXPORTER_VERSION
from st_exporter.blank_columns import check_blank_columns
from st_exporter.config import ExporterSettings
from st_exporter.denormalize import apply_customer_contacts, build_job_rows, customer_ids
from st_exporter.feeds.appointments import fetch_appointments_delta
from st_exporter.feeds.assignments import fetch_assignments_delta
from st_exporter.feeds.contacts import (
    CURSOR_KEY as CONTACTS_CURSOR_KEY,
)
from st_exporter.feeds.contacts import (
    DEFAULT_MAX_CONTACT_CUSTOMERS,
    ROUTE_EXPORT,
    ROUTE_PER_CUSTOMER,
    fetch_contacts_export_delta,
    fetch_contacts_per_customer,
    group_contacts_by_customer,
)
from st_exporter.feeds.customers import fetch_customers_delta
from st_exporter.feeds.financial import (
    DEFAULT_MAX_TIMESHEET_JOBS,
    fetch_completed_job_ids,
    fetch_invoices,
    fetch_job_costs,
    fetch_timesheets,
)
from st_exporter.feeds.financial import fetch_business_units as fetch_business_unit_list
from st_exporter.feeds.jobs import fetch_jobs_delta
from st_exporter.feeds.locations import fetch_locations_delta
from st_exporter.feeds.pricebook import (
    ITEM_RESOURCES,
    fetch_pricebook_categories,
    fetch_pricebook_items,
)
from st_exporter.feeds.raw_cache import RawCache
from st_exporter.feeds.reference import fetch_business_units, fetch_job_types, fetch_technicians
from st_exporter.financial import (
    CONTRACT_VERSION as FINANCIAL_CONTRACT_VERSION,
)
from st_exporter.financial import (
    build_business_unit_grid,
    build_invoice_grid,
    build_job_cost_grid,
    build_timesheet_grid,
)
from st_exporter.format import (
    JOBS_CONTRACT_VERSION,
    TECHNICIANS_CONTRACT_VERSION,
    build_job_grid,
    build_technician_grid,
)
from st_exporter.images.client import TrueQuoteImageClient
from st_exporter.images.ledger import ImageLedger
from st_exporter.images.upload import ImageUploadSummary, upload_pricebook_images
from st_exporter.logging_setup import announce_to_actions, configure_logging, logger
from st_exporter.meta import (
    CursorBundle,
    MetaRow,
    MetaRowSet,
    build_meta_grid,
    parse_meta_grid,
)
from st_exporter.pricebook import CONTRACT_VERSION, build_category_grid, build_item_grid
from st_exporter.scopes import ScopeLedger
from st_exporter.sheets import SheetsClient, SheetsPort, get_gspread_client
from st_exporter.window import DEFAULT_WINDOW_DAYS, FINANCIAL_WINDOW_DAYS, in_window
from st_exporter.writeback import JobsWriteBack

_RAW_CUSTOMERS = "_raw_customers"
_RAW_LOCATIONS = "_raw_locations"
_RAW_JOBS = "_raw_jobs"
_RAW_APPOINTMENTS = "_raw_appointments"
_RAW_ASSIGNMENTS = "_raw_assignments"
#: Only written on the opt-in bulk contacts route — the per-customer route has no
#: change feed to cache and re-reads each run, so a stale number can never be
#: exported. See feeds/contacts.py.
_RAW_CUSTOMER_CONTACTS = "_raw_customer_contacts"

# `outbox` is not an export feed — it writes no tab and fetches nothing. It is
# named here because naming it is what makes the outbox drain OPT-IN: the drain
# runs if and only if this exact word is in `--feeds`, so the mere presence of a
# product's secrets in some other workflow job can no longer cause a second
# drain of the same queue. See EXPORT_FEEDS / OUTBOX_FEED below and cli.py.
OUTBOX_FEED = "outbox"
EXPORT_FEEDS = frozenset({"jobs", "technicians", "pricebook", "financial"})
_VALID_FEEDS = EXPORT_FEEDS | {OUTBOX_FEED}
_FEED_LIST = "jobs, technicians, pricebook, financial, outbox"
# Neither `pricebook` nor `financial` is a default. `pricebook` is a catalogue on a
# much slower cadence than jobs (~5 min) and technicians (~30 min), and re-listing
# it on every jobs run would be pure waste. `financial` is a six-hourly feed —
# matching the cadence of the Profit Wizard cron it replaces — and its job-costing
# half runs a ServiceTitan report, which is throttled to roughly one run per minute
# per tenant, so putting it on the jobs cadence would throttle the tenant outright.
DEFAULT_FEEDS = frozenset({"jobs", "technicians"})

# The four tabs the single `pricebook` feed writes, tab name -> ServiceTitan
# resource. Each gets its own `_meta` row keyed by the tab name.
PRICEBOOK_TABS: dict[str, str] = {f"pricebook.{resource}": resource for resource in ITEM_RESOURCES}
PRICEBOOK_CATEGORIES_TAB = "pricebook.categories"
PRICEBOOK_FEED_NAMES: tuple[str, ...] = tuple(PRICEBOOK_TABS) + (PRICEBOOK_CATEGORIES_TAB,)

# The four tabs the single `financial` feed writes. The names are Profit Wizard's,
# not this exporter's — its reader (`lib/hosted/tabs.ts`) addresses these exact
# strings, so they are part of the contract, dots and camelCase included.
FINANCIAL_INVOICES_TAB = "accounting.invoices"
FINANCIAL_TIMESHEETS_TAB = "payroll.timesheets"
FINANCIAL_BUSINESS_UNITS_TAB = "settings.businessUnits"
FINANCIAL_JOB_COSTS_TAB = "reporting.jobCosts"
FINANCIAL_FEED_NAMES: tuple[str, ...] = (
    FINANCIAL_INVOICES_TAB,
    FINANCIAL_TIMESHEETS_TAB,
    FINANCIAL_BUSINESS_UNITS_TAB,
    FINANCIAL_JOB_COSTS_TAB,
)

# Every export tab this exporter can write — the unit a ServiceTitan permission is
# granted over, and therefore the unit a 403 is classified over. `scopes` keys its
# permission strings by these exact names; a test holds the two in step.
EXPORT_TABS: tuple[str, ...] = ("jobs", "technicians") + PRICEBOOK_FEED_NAMES + FINANCIAL_FEED_NAMES


def parse_feeds(value: str) -> frozenset[str]:
    """Parse a comma-separated --feeds value into a validated set.

    Blank segments are dropped so "jobs," or " jobs , technicians " both work —
    the reusable workflow's `feeds` input is free-text, not a strict enum.
    """
    feeds = frozenset(part.strip() for part in value.split(",") if part.strip())
    if not feeds:
        raise ConfigError(f"--feeds must name at least one of: {_FEED_LIST}.")
    unknown = feeds - _VALID_FEEDS
    if unknown:
        raise ConfigError(
            f"unknown feed(s): {', '.join(sorted(unknown))}. Valid feeds: {_FEED_LIST}."
        )
    return feeds


@dataclass
class ExportSummary:
    jobs_row_count: int
    technicians_row_count: int
    skipped_no_job: int
    dry_run: bool
    # Row count per `pricebook.*` tab, or None when the pricebook feed wasn't
    # selected this run. None and {} are different: {} means the feed ran and
    # wrote no tab at all — every tab failed, or every one was refused for want of
    # its own Pricebook entity (`scope_not_granted`/`scope_revoked`). A tab that IS
    # written always has at least its header, so it is never 0-vs-{}.
    pricebook_row_counts: dict[str, int] | None = None
    # Tab name -> why it was skipped, for the pricebook tabs that failed. Same
    # contract as `financial_failures`: a failed tab is absent from
    # `pricebook_row_counts` and present here, and its previous contents and
    # `_meta` row are left untouched in the Sheet.
    pricebook_failures: dict[str, str] | None = None
    # What the image upload pass did, or None when it didn't run (pricebook feed
    # not selected, no `image_upload` machine token configured, or dry-run).
    images: ImageUploadSummary | None = None
    # Row count per successfully-written `financial` tab, or None when the feed
    # wasn't selected. A tab that FAILED is absent from this dict and present in
    # `financial_failures` — the two together always name all four tabs, so
    # "wrote 0 rows" can never be confused with "didn't manage to write".
    financial_row_counts: dict[str, int] | None = None
    # Tab name -> why it was skipped, for the financial tabs that failed. Its
    # previous contents and its `_meta` row are left untouched in the Sheet.
    financial_failures: dict[str, str] | None = None
    # Feed name -> why it failed, for the two top-level feeds (`jobs`,
    # `technicians`). A feed named here was NOT written this run and its `_meta`
    # row — cursor included — was carried forward unchanged, so the next run
    # re-fetches from the same point. None when both ran (or weren't selected).
    feed_failures: dict[str, str] | None = None
    # TAB -> the ServiceTitan permission it needs, for a tab this tenant has NEVER
    # been granted (403, and no `_meta` row and no tab in the Sheet has ever
    # evidenced a successful run of it). Keyed per tab, not per feed, because
    # ServiceTitan grants per entity: a TrueQuote-only tenant is refused
    # `pricebook.materials` on every run and reads the other three pricebook tabs
    # perfectly well, and that is the ORDINARY state, not an edge case. A quiet,
    # expected skip: that tab is not written, nothing failed, its siblings are
    # unaffected, and an absent tab means the contractor did not buy it.
    scope_not_granted: dict[str, str] = field(default_factory=dict)
    # TAB -> ServiceTitan's 403 detail, for a tab that HAS been written on this
    # Sheet before. The permission was taken away: the run is annotated at
    # `::error` and exits non-zero (`cli.py`). That tab and its `_meta` row are
    # left exactly as the last good run left them; its siblings still refresh.
    scope_revoked: dict[str, str] = field(default_factory=dict)
    # The `jobs` tab of this run, held open so the outbox drain that follows can
    # write its own assignments straight into it (ticket 17) instead of waiting a
    # whole cycle to rediscover them. None whenever that is not possible or not
    # wanted: no `jobs` feed this run (a drain-only run must make no Export Store
    # round-trip — see `JobsWriteBack`), or a dry run, which writes nothing at all.
    #
    # It is a live handle rather than data because applying it must happen AFTER
    # the drain, and the drain happens after `run_export` has returned. `cli.py`
    # is what joins the two.
    jobs_write_back: JobsWriteBack | None = None


def run_export(
    st_settings: Settings,
    exporter_settings: ExporterSettings,
    *,
    feeds: frozenset[str] = DEFAULT_FEEDS,
    dry_run: bool = False,
    client: ServiceTitanClient | None = None,
    export_store: SheetsPort | None = None,
    raw_cache_store: SheetsPort | None = None,
    image_client: TrueQuoteImageClient | None = None,
) -> ExportSummary:
    """Run one export: fetch feeds, denormalise, window-filter, write the Sheets.

    ``feeds`` selects which output tab(s) this call fetches and writes — either
    subset lets the reusable workflow honor the spec's different cadences (jobs
    ~5 min, technicians ~30 min) without re-fetching the unneeded feed every
    time. The feed not selected is left byte-for-byte untouched in both the
    Export Store's tab and its `_meta` row.

    ``pricebook`` is one selectable feed that writes four tabs
    (`pricebook.services`/`equipment`/`materials`/`categories`), each with its own
    `_meta` row — see ``pricebook.py`` for the frozen column contract.

    ``financial`` is likewise one selectable feed writing four tabs
    (`accounting.invoices`, `payroll.timesheets`, `settings.businessUnits`,
    `reporting.jobCosts`) for Profit Wizard — see ``financial.py``. Its four tabs
    are independent: one failing leaves the other three written and keeps the
    failed tab's previous contents and `_meta` row.

    ``client``/``export_store``/``raw_cache_store`` can be injected (used by tests
    with fixtures and an in-memory Sheets double); left as ``None`` in production,
    where real ones are constructed from ``st_settings``/``exporter_settings``.
    """
    configure_logging()

    # Taken here, not in the image pass: the budget is the whole run's share of
    # the job, and the pricebook export that precedes the pass spends minutes of
    # it. Measuring from the pass's own start would let a slow export push the
    # pass straight through the runner's `timeout-minutes`, which is the failure
    # the budget exists to prevent.
    image_deadline = monotonic() + exporter_settings.image_budget_seconds

    owns_client = client is None
    active_client = client or ServiceTitanClient(st_settings)
    try:
        active_export_store, active_raw_cache_store = _resolve_stores(
            exporter_settings, export_store, raw_cache_store, dry_run=dry_run
        )
        return _run(
            active_client,
            active_export_store,
            active_raw_cache_store,
            feeds=feeds,
            window_days=exporter_settings.window_days,
            financial_window_days=exporter_settings.financial_window_days,
            financial_max_jobs=exporter_settings.financial_max_jobs,
            contacts_route=exporter_settings.contacts_route,
            contacts_max_customers=exporter_settings.contacts_max_customers,
            pricebook_category_ids=exporter_settings.pricebook_category_ids,
            image_client=image_client,
            image_deadline=image_deadline,
            dry_run=dry_run,
        )
    finally:
        if owns_client:
            active_client.close()


def _resolve_stores(
    exporter_settings: ExporterSettings,
    export_store: SheetsPort | None,
    raw_cache_store: SheetsPort | None,
    *,
    dry_run: bool,
) -> tuple[SheetsPort, SheetsPort]:
    if export_store is not None and raw_cache_store is not None:
        return export_store, raw_cache_store
    # dry_run does NOT substitute in-memory stores here — it needs to read the
    # real cursor/raw-cache state to be a genuine preview of the next real run
    # (accurate delta-only fetch counts), not always behave like a first run.
    # _run() already gates every write behind `if dry_run`, so opening real
    # stores is still write-free.
    gc = get_gspread_client(exporter_settings.service_account_json)
    resolved_export_store = export_store or SheetsClient.open(gc, exporter_settings.sheet_id)
    resolved_raw_cache_store = raw_cache_store or SheetsClient.open(
        gc, exporter_settings.raw_cache_sheet_id
    )
    return resolved_export_store, resolved_raw_cache_store


def _run(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    raw_cache_store: SheetsPort,
    *,
    feeds: frozenset[str] = DEFAULT_FEEDS,
    window_days: int = DEFAULT_WINDOW_DAYS,
    financial_window_days: int = FINANCIAL_WINDOW_DAYS,
    financial_max_jobs: int = DEFAULT_MAX_TIMESHEET_JOBS,
    contacts_route: str = ROUTE_PER_CUSTOMER,
    contacts_max_customers: int = DEFAULT_MAX_CONTACT_CUSTOMERS,
    pricebook_category_ids: tuple[str, ...] = (),
    image_client: TrueQuoteImageClient | None = None,
    # None = no budget. Only a caller that knows nothing will kill the process
    # may leave it unset; `run_export` always sets one.
    image_deadline: float | None = None,
    dry_run: bool,
) -> ExportSummary:
    now = datetime.now(timezone.utc)
    today = now.date()
    run_at = now.isoformat()

    meta_rows = parse_meta_grid(export_store.read_grid("_meta"))
    # Keyed, not appended: a tab reached by both doors (written fresh, and carried
    # forward because a sibling was refused) must still end up with exactly ONE
    # row. See `MetaRowSet`.
    new_meta_rows = MetaRowSet()
    # Which feeds the tenant's ServiceTitan app is allowed to read is decided by
    # ServiceTitan, not by the caller workflow: every feed job runs on its
    # schedule, and a 403 is the answer for a product this contractor did not
    # buy. `scopes.py` is what tells that apart from a permission that was
    # revoked, using the `_meta` rows read above — so it is built from the state
    # BEFORE this run touches it.
    scopes = ScopeLedger(
        meta_rows=meta_rows,
        new_meta_rows=new_meta_rows,
        # Second-line evidence for a tab with no `_meta` row: a deleted or renamed
        # `_meta` tab reads as `[]`, which would otherwise make every later 403
        # "never bought". Lazy — only a 403 on a row-less tab ever reads a grid.
        tab_exists=lambda tab: bool(export_store.read_grid(tab)),
    )

    # `jobs` and `technicians` are independent feeds behind the same guard the
    # pricebook and financial tabs already had. Before ticket 21 they were
    # unguarded, and because `_meta` is written ONCE for all feeds at the end, a
    # failure in the later of the two threw past the `_meta` write and discarded
    # the cursor of the earlier one — whose tab was already on disk.
    feed_failures: dict[str, str] = {}

    windowed_rows: list[dict[str, Any]] = []
    jobs_write_back: JobsWriteBack | None = None
    skipped_no_job = 0
    jobs_row_count = meta_rows["jobs"].row_count if "jobs" in meta_rows else 0

    if "jobs" in feeds:
        jobs_outcome = _guarded_feed(
            "jobs",
            meta_rows=meta_rows,
            new_meta_rows=new_meta_rows,
            failures=feed_failures,
            consequence=_CURSOR_STUCK,
            scopes=scopes,
            run=lambda: _run_jobs_feed(
                client,
                export_store,
                raw_cache_store,
                meta_rows=meta_rows,
                new_meta_rows=new_meta_rows,
                today=today,
                run_at=run_at,
                window_days=window_days,
                contacts_route=contacts_route,
                contacts_max_customers=contacts_max_customers,
                dry_run=dry_run,
            ),
        )
        if jobs_outcome is not None:
            windowed_rows, skipped_no_job = jobs_outcome
            jobs_row_count = len(windowed_rows)
            if not dry_run:
                # The one door out of `_guarded_feed` that means "the tab on disk
                # is THIS run's rows": a non-None outcome. The guard returns None
                # for every other exit — a 403 (never granted, or revoked) and any
                # other `STCLIError` alike — so a write-back handle cannot exist
                # for a feed that wrote nothing. That is the whole precondition,
                # and it is why the construction sits here rather than beside the
                # call: a scope-denied `jobs` is a tab that does not exist on this
                # Sheet (never granted) or one frozen at its last good run
                # (revoked), and a write-back that replaced it would create a tab
                # ServiceTitan just refused us the data for, out of rows this run
                # never fetched — resurrecting a tab a quiet skip means to leave
                # absent, and overwriting a revoked tab's preserved contents.
                #
                # Same rows, same builder, same tab — see `writeback.JobsWriteBack`.
                # Created here and applied by `cli.py` after the drain, so no
                # write-back can come between a written tab and the `_meta` row
                # that describes it.
                jobs_write_back = JobsWriteBack(export_store, windowed_rows)
        # Otherwise no tab was written — because the feed failed, or because a 403
        # ruled the tab out — and `jobs_row_count` keeps the carried-forward
        # `_meta` count, which is what the untouched tab still holds.
    elif "jobs" in meta_rows:
        new_meta_rows.carry(meta_rows["jobs"])

    technicians_row_count = meta_rows["technicians"].row_count if "technicians" in meta_rows else 0

    if "technicians" in feeds:
        technicians_outcome = _guarded_feed(
            "technicians",
            meta_rows=meta_rows,
            new_meta_rows=new_meta_rows,
            failures=feed_failures,
            # A full-replace feed with no cursor of its own: nothing re-drains,
            # the tab simply stays at its previous contents.
            consequence=_TAB_STALE,
            scopes=scopes,
            run=lambda: _run_technicians_feed(
                client,
                export_store,
                new_meta_rows=new_meta_rows,
                run_at=run_at,
                dry_run=dry_run,
            ),
        )
        if technicians_outcome is not None:
            technicians_row_count = technicians_outcome
    elif "technicians" in meta_rows:
        new_meta_rows.carry(meta_rows["technicians"])

    pricebook_row_counts: dict[str, int] | None = None
    pricebook_failures: dict[str, str] | None = None
    image_summary: ImageUploadSummary | None = None
    pricebook_item_records: list[dict[str, Any]] | None = None
    pricebook_catalogue_complete = False
    if "pricebook" in feeds:
        (
            pricebook_row_counts,
            pricebook_failures,
            pricebook_item_records,
            pricebook_catalogue_complete,
        ) = _run_pricebook_feed(
            client,
            export_store,
            meta_rows=meta_rows,
            new_meta_rows=new_meta_rows,
            run_at=run_at,
            category_ids=pricebook_category_ids,
            scopes=scopes,
            dry_run=dry_run,
        )
    else:
        for feed_name in PRICEBOOK_FEED_NAMES:
            if feed_name in meta_rows:
                new_meta_rows.carry(meta_rows[feed_name])

    financial_row_counts: dict[str, int] | None = None
    financial_failures: dict[str, str] | None = None
    if "financial" in feeds:
        financial_row_counts, financial_failures = _run_financial_feed(
            client,
            export_store,
            meta_rows=meta_rows,
            new_meta_rows=new_meta_rows,
            run_at=run_at,
            today=today,
            window_days=financial_window_days,
            max_jobs=financial_max_jobs,
            scopes=scopes,
            dry_run=dry_run,
        )
    else:
        for feed_name in FINANCIAL_FEED_NAMES:
            if feed_name in meta_rows:
                new_meta_rows.carry(meta_rows[feed_name])

    if dry_run:
        logger.info(
            "dry-run: would write jobs=%d technicians=%d pricebook=%s financial=%s "
            "(nothing written)",
            jobs_row_count,
            technicians_row_count,
            pricebook_row_counts,
            financial_row_counts,
        )
    else:
        # ONE write for every feed, and every feed above is guarded, so this line
        # is now reached whatever ServiceTitan does. That is what makes a
        # committed feed's cursor survive a later feed's failure — and it is why
        # the guards matter more than this line's position: moving the `_meta`
        # write earlier, per feed, would let a cursor land for a tab whose write
        # had not happened yet. The safe shape is "always reached, always last".
        export_store.replace_grid("_meta", build_meta_grid(new_meta_rows))

    # The image pass runs AFTER `_meta`, deliberately and structurally. It is a
    # side lane: it writes no export tab, and the four pricebook tabs it draws
    # its work from are already written. Running it here means nothing it does —
    # not an exception, not a Sheets 429 on the ledger, not a forty-minute
    # download stall against `timeout-minutes` — can come between a written tab
    # and the `_meta` row that describes it. Handling those failures is not
    # enough; ordering makes the whole class of "fresh tabs, stale `_meta`"
    # impossible. Never move it back above the `_meta` write.
    if pricebook_item_records is not None:
        # The pass runs whenever the pricebook feed ran. It is NOT gated on a
        # permission answer: `Pricebook -> Materials` refused says nothing about
        # the services and equipment images a TrueQuote tenant is paying for, and
        # gating this on any scope denial was why such a tenant never received a
        # single image. The one thing a 403 must veto is the PRUNE, and
        # `catalogue_complete` below already carries exactly that fact — a tab
        # that was not read, for any reason, means its items never reached
        # `item_records`, so "not looked at" must not be read as "gone".
        image_summary = _upload_pricebook_images(
            client,
            raw_cache_store,
            pricebook_item_records,
            image_client=image_client,
            image_deadline=image_deadline,
            run_at=run_at,
            dry_run=dry_run,
            # An item tab that did not produce a grid — failed, or refused with a
            # 403 — means `item_records` is missing that tab's items, so this pass
            # did NOT see the whole catalogue however well it ran.
            catalogue_complete=pricebook_catalogue_complete,
        )

    return ExportSummary(
        feed_failures=feed_failures or None,
        jobs_row_count=jobs_row_count,
        technicians_row_count=technicians_row_count,
        skipped_no_job=skipped_no_job,
        dry_run=dry_run,
        pricebook_row_counts=pricebook_row_counts,
        pricebook_failures=pricebook_failures,
        images=image_summary,
        financial_row_counts=financial_row_counts,
        financial_failures=financial_failures,
        scope_not_granted=dict(scopes.not_granted),
        scope_revoked=dict(scopes.revoked),
        jobs_write_back=jobs_write_back,
    )


#: What a failing feed costs the NEXT run, per kind of feed. A cursor-tracked feed
#: that fails does not advance its cursor, so the next run re-fetches from the same
#: point; while the failure persists, EVERY run re-drains the whole change feed from
#: the beginning. That is correct but unboundedly slow, and — until this text is
#: emitted — completely invisible. See ticket 21: a 403 on technicians used to strand
#: `_meta` after the jobs tab had already been written, and it presented as a slow
#: exporter rather than as an error.
_CURSOR_STUCK = (
    "Its cursor did NOT advance, so the next run re-fetches from the same point. "
    "While this keeps failing, every run re-drains the whole change feed from the "
    "beginning, which presents as a slow exporter rather than as an error."
)
_TAB_STALE = (
    "Its tab and its _meta row are unchanged from the last run that succeeded, so "
    "consumers keep reading the previous contents until this is fixed."
)

_T = TypeVar("_T")


def _announce_feed_failure(feed: str, exc: Exception, consequence: str) -> None:
    """Log AND surface one feed's failure where a human will actually see it.

    A WARNING in the log of a run that still exits 0 is invisible — the run is
    green and nobody opens a green run. So the same message also goes out as a
    GitHub Actions annotation and a step-summary line, exactly as the blank-column
    detector does (``logging_setup.announce_to_actions``). Naming the CONSEQUENCE,
    not just the error, is the point: "403 on technicians" reads as a small local
    problem, while "the cursor did not advance and every run now re-drains" is the
    thing somebody has to act on.

    At ``::error``, not ``::warning``: a whole top-level feed did not export, and
    ``cli.py`` now exits non-zero for exactly this (``summary.feed_failures``). A
    yellow annotation on a red run reads as a suspicion to check later; this is
    the reason the run failed, and the annotation is the only place the run says
    WHICH feed and why without anyone opening the log.
    """
    logger.warning("FEED FAILED: %s was not refreshed this run (%s). %s", feed, exc, consequence)
    announce_to_actions(
        "Feed failed",
        f"{feed} was not refreshed this run ({exc}). {consequence}",
        level="error",
    )


def _guarded_feed(
    feed: str,
    *,
    meta_rows: dict[str, MetaRow],
    new_meta_rows: MetaRowSet,
    failures: dict[str, str],
    consequence: str,
    scopes: ScopeLedger,
    run: Callable[[], _T],
) -> _T | None:
    """Run one top-level feed so that its failure costs exactly that feed.

    The same contract ``_TabGuard`` gives the pricebook and financial tabs, lifted
    to the two feeds that never had it: a feed that fails is not written, its
    previous `_meta` row is carried forward unchanged (so ``last_run_at`` still
    says when it was last genuinely refreshed), and — crucially — the feeds that
    already succeeded keep the `_meta` rows they appended, including their
    cursors. ``_run`` writes `_meta` once for all feeds, so the ONLY way a
    committed feed could lose its cursor was a later feed throwing past this
    point.

    Only ``STCLIError`` is caught, which is complete: ``st_cli.client`` wraps
    transport failures in ``TransportError``. A ``KeyError`` out of the row
    mapping is a bug in this repo and must still crash the run loudly — and
    crashing is the SAFE direction, because an unwritten `_meta` only costs a
    re-drain.

    Any `_meta` rows the feed had already recorded before it threw are DISCARDED
    (that is what ``committed`` is for) before the previous row is carried
    forward. Each feed records its own row last, once its tab is on disk, so
    there should never be one to discard — but "should" is how a cursor comes to
    describe a tab that was never written, which is the one failure this ticket
    must not trade itself for. Belt and braces, and it matters more than it looks
    now that ``MetaRowSet.carry`` is a ``setdefault``: a stale fresh row left in
    place would silently BEAT the row we are trying to carry forward, which is
    the cursor-leads-the-data direction again by another door.

    A **403 is not this feed's failure but its permission**, and goes to the
    ``ScopeLedger`` instead (a tab never granted is skipped quietly and the run
    stays green; one whose permission was revoked is loud and reds the run). The
    rollback happens first either way: whichever door the tab leaves by, it must
    leave no half-written `_meta` row behind. Every other ``STCLIError`` — the
    400 from `active=Any`, a 429 storm, a transport failure — keeps the guarded
    behaviour above, which is what stops an outage being filed as a product the
    contractor never bought.

    Returns ``None`` when the feed did not write its tab, for either reason; the
    caller keeps whatever it had.
    """
    committed = new_meta_rows.snapshot()
    try:
        return run()
    except STCLIError as exc:
        new_meta_rows.restore(committed)
        if scopes.deny(feed, exc) is not None:
            # Classified, announced and carried forward by the ledger. Not a
            # failure of this feed, so it is not named in `failures`.
            return None
        failures[feed] = str(exc)
        _announce_feed_failure(feed, exc, consequence)
        if feed in meta_rows:
            new_meta_rows.carry(meta_rows[feed])
        return None


def _optional_reference(
    label: str,
    fetch: Callable[[], dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Fetch a reference lookup the jobs feed can do without; ``{}`` if it fails.

    ``denormalize`` uses job types and business units only as a FALLBACK, for jobs
    whose own record carries no ``jobTypeName``/``businessUnitName``. A 403 on
    either endpoint used to abort the entire jobs feed — killing the run for data
    it may not even have needed. Degrading to an empty lookup costs, at worst, a
    blank ``job_type``/``business_unit`` column, which the blank-column detector
    then also reports. The degradation is announced, so it is never silent.
    """
    try:
        return fetch()
    except STCLIError as exc:
        logger.warning(
            "DEGRADED: could not read %s (%s). The jobs feed continues without it; "
            "job rows fall back to the name on the job record, so job_type / "
            "business_unit may be blank for jobs that carry only an id.",
            label,
            exc,
        )
        announce_to_actions(
            "Reference lookup degraded",
            f"could not read {label} ({exc}). The jobs feed still ran; job_type / "
            f"business_unit may be blank for jobs that carry only an id.",
        )
        return {}


def _customer_contacts(
    client: ServiceTitanClient,
    rows: list[dict[str, Any]],
    *,
    route: str,
    max_customers: int,
    raw_cache_store: SheetsPort,
    cursor: str | None,
    dry_run: bool,
) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """Customer phone/email for the rows about to be written; ``{}`` if refused.

    The same degradation `_optional_reference` gives job types and business
    units, for the same reason and by the same two mechanisms (a WARNING plus an
    Actions annotation): `customer_phone` and `customer_email` are one cell each,
    and losing the whole `jobs` feed over them would cost a contractor every
    column on every row rather than two. A 403 here — the CRM permission this
    sub-resource needs not being ticked — therefore leaves those two cells at
    whatever the customer record itself resolved (usually blank) and the tab
    otherwise intact.

    **The failure is never silent.** It is announced, and the blank-column
    detector reports the two columns independently if they end up empty tab-wide,
    so "contacts were refused" and "contacts are blank" both reach the run
    summary rather than only the log of a green run.

    Returns the contacts and the contacts cursor to persist. On the default
    per-customer route there is no cursor to advance, so the previous value is
    handed straight back; on the bulk route a failure hands back the PREVIOUS
    cursor too, so nothing is skipped — same trailing-edge rule as every other
    feed here.
    """
    try:
        if route == ROUTE_EXPORT:
            try:
                return _bulk_customer_contacts(
                    client, raw_cache_store=raw_cache_store, cursor=cursor, dry_run=dry_run
                )
            except APIError as exc:
                if exc.status_code not in (400, 404):
                    raise
                # ServiceTitan saying "there is no such feed" — which is the one
                # thing nobody could establish without a tenant. Say so loudly
                # (it is the answer to an open question, not just an error) and
                # serve this run from the route that is known to work.
                logger.warning(
                    "DEGRADED: the bulk crm/export/customers/contacts feed answered %s. "
                    "This tenant does not have it; falling back to the per-customer "
                    "route for this run. Set EXPORTER_CONTACTS_ROUTE=per-customer to "
                    "stop asking.",
                    exc,
                )
                announce_to_actions(
                    "Bulk contacts feed absent",
                    f"crm/export/customers/contacts answered {exc}; this tenant does not "
                    f"have that feed. The run fell back to the per-customer contacts "
                    f"route. Set EXPORTER_CONTACTS_ROUTE=per-customer.",
                )
        return (
            fetch_contacts_per_customer(client, customer_ids(rows), max_customers=max_customers),
            cursor,
        )
    except STCLIError as exc:
        logger.warning(
            "DEGRADED: could not read customer contacts (%s). The jobs feed continues "
            "without them; customer_phone / customer_email fall back to whatever the "
            "customer record itself carries, which is usually blank. Check the CRM "
            "customer-contacts permission on this tenant's ServiceTitan app.",
            exc,
        )
        announce_to_actions(
            "Customer contacts degraded",
            f"could not read customer contacts ({exc}). The jobs tab still exported; "
            f"customer_phone / customer_email may be blank on every row. Check the CRM "
            f"customer-contacts permission on this tenant's ServiceTitan app.",
        )
        return {}, cursor


def _bulk_customer_contacts(
    client: ServiceTitanClient,
    *,
    raw_cache_store: SheetsPort,
    cursor: str | None,
    dry_run: bool,
) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """The opt-in bulk route: a cursor-tracked change feed, cached like the rest.

    Identical treatment to the five feeds above — delta from the stored cursor,
    merged last-write-wins into a `_raw_customer_contacts` tab, grouped by
    ``customerId`` — because if this feed exists it IS one of them. The cursor is
    written by the caller alongside the other five, after the tab is on disk.
    """
    raw_contacts = RawCache.from_grid(raw_cache_store.read_grid(_RAW_CUSTOMER_CONTACTS))
    delta, next_cursor = fetch_contacts_export_delta(client, cursor)
    raw_contacts.merge(delta)
    logger.info("fetched deltas: customer-contacts=%d (bulk route)", len(delta))
    if not dry_run:
        raw_cache_store.replace_grid(_RAW_CUSTOMER_CONTACTS, raw_contacts.to_grid())
    return group_contacts_by_customer(raw_contacts.values()), next_cursor


def _run_jobs_feed(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    raw_cache_store: SheetsPort,
    *,
    meta_rows: dict[str, MetaRow],
    new_meta_rows: MetaRowSet,
    today: Any,
    run_at: str,
    window_days: int,
    contacts_route: str = ROUTE_PER_CUSTOMER,
    contacts_max_customers: int = DEFAULT_MAX_CONTACT_CUSTOMERS,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch/denormalise/window the jobs feed; return (windowed_rows, skipped_no_job).

    Mutates ``new_meta_rows`` in place (appends the jobs MetaRow) so the caller
    doesn't need a second merge step — mirrors how the technicians half works.
    """
    jobs_meta = meta_rows.get("jobs")
    cursor_bundle = CursorBundle.decode(jobs_meta.last_cursor if jobs_meta else None)

    raw_customers = RawCache.from_grid(raw_cache_store.read_grid(_RAW_CUSTOMERS))
    raw_locations = RawCache.from_grid(raw_cache_store.read_grid(_RAW_LOCATIONS))
    raw_jobs = RawCache.from_grid(raw_cache_store.read_grid(_RAW_JOBS))
    raw_appointments = RawCache.from_grid(raw_cache_store.read_grid(_RAW_APPOINTMENTS))
    raw_assignments = RawCache.from_grid(raw_cache_store.read_grid(_RAW_ASSIGNMENTS))

    customers_delta, customers_cursor = fetch_customers_delta(
        client, cursor_bundle.get("customers")
    )
    locations_delta, locations_cursor = fetch_locations_delta(
        client, cursor_bundle.get("locations")
    )
    jobs_delta, jobs_cursor = fetch_jobs_delta(client, cursor_bundle.get("jobs"))
    appointments_delta, appointments_cursor = fetch_appointments_delta(
        client, cursor_bundle.get("appointments")
    )
    assignments_delta, assignments_cursor = fetch_assignments_delta(
        client, cursor_bundle.get("assignments")
    )

    raw_customers.merge(customers_delta)
    raw_locations.merge(locations_delta)
    raw_jobs.merge(jobs_delta)
    raw_appointments.merge(appointments_delta)
    raw_assignments.merge(assignments_delta)

    logger.info(
        "fetched deltas: customers=%d locations=%d jobs=%d appointments=%d assignments=%d",
        len(customers_delta),
        len(locations_delta),
        len(jobs_delta),
        len(appointments_delta),
        len(assignments_delta),
    )

    job_types = _optional_reference("job types", lambda: fetch_job_types(client))
    business_units = _optional_reference("business units", lambda: fetch_business_units(client))

    denormalized = build_job_rows(
        raw_jobs,
        raw_appointments,
        raw_assignments,
        raw_customers,
        raw_locations,
        job_types,
        business_units,
    )
    if denormalized.skipped_no_job:
        logger.info(
            "skipped %d appointment(s) whose job hasn't arrived in the raw cache yet",
            denormalized.skipped_no_job,
        )

    windowed_rows, skipped_no_start, skipped_bad_timestamp = _apply_window(
        denormalized.rows, today=today, window_days=window_days
    )
    if skipped_no_start:
        logger.info("skipped %d appointment(s) with no appointment_start at all", skipped_no_start)
    if skipped_bad_timestamp:
        logger.info(
            "skipped %d appointment(s) with an unparsable appointment_start "
            "(excluded rather than crashing the run)",
            skipped_bad_timestamp,
        )

    # Contact details are fetched for the WINDOWED rows only — the customers whose
    # jobs actually reach the tab — and applied on top of whatever the customer
    # record's own fields resolved. Blank cells if it is refused; never a failed
    # feed. See `_customer_contacts`.
    contacts_by_customer, contacts_cursor = _customer_contacts(
        client,
        windowed_rows,
        route=contacts_route,
        max_customers=contacts_max_customers,
        raw_cache_store=raw_cache_store,
        cursor=cursor_bundle.get(CONTACTS_CURSOR_KEY),
        dry_run=dry_run,
    )
    apply_customer_contacts(windowed_rows, contacts_by_customer)

    jobs_grid = build_job_grid(windowed_rows)
    check_blank_columns("jobs", jobs_grid)

    new_cursor_bundle = CursorBundle(
        {
            "customers": customers_cursor,
            "locations": locations_cursor,
            "jobs": jobs_cursor,
            "appointments": appointments_cursor,
            "assignments": assignments_cursor,
            CONTACTS_CURSOR_KEY: contacts_cursor,
        }
    )
    if not dry_run:
        _prune_raw_caches(
            denormalized.rows,
            windowed_rows,
            raw_appointments=raw_appointments,
            raw_assignments=raw_assignments,
        )
        raw_cache_store.replace_grid(_RAW_CUSTOMERS, raw_customers.to_grid())
        raw_cache_store.replace_grid(_RAW_LOCATIONS, raw_locations.to_grid())
        raw_cache_store.replace_grid(_RAW_JOBS, raw_jobs.to_grid())
        raw_cache_store.replace_grid(_RAW_APPOINTMENTS, raw_appointments.to_grid())
        raw_cache_store.replace_grid(_RAW_ASSIGNMENTS, raw_assignments.to_grid())
        export_store.replace_grid("jobs", jobs_grid)

    # The cursor is appended only once the tab it describes is on disk, and it is
    # the LAST thing this function does. The two directions are not symmetric: a
    # cursor that lags the data costs a re-fetch of a window already merged (the
    # raw caches are keyed by id, so re-merging is idempotent), while a cursor
    # that leads the data skips a window of changes that nothing will ever fetch
    # again. Slow is recoverable; skipped is permanent. So the cursor must always
    # be the trailing edge.
    new_meta_rows.add(
        MetaRow(
            feed="jobs",
            last_run_at=run_at,
            last_cursor=new_cursor_bundle.encode(),
            row_count=len(windowed_rows),
            exporter_version=EXPORTER_VERSION,
            contract_version=JOBS_CONTRACT_VERSION,
        )
    )

    return windowed_rows, denormalized.skipped_no_job


def _run_technicians_feed(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    *,
    new_meta_rows: MetaRowSet,
    run_at: str,
    dry_run: bool,
) -> int:
    """Fetch technicians and (unless dry-run) write the tab; append its MetaRow.

    Returns the number of rows WRITTEN, derived from the grid rather than from the
    fetched records — ``build_technician_grid`` dedupes, so counting the records
    would report rows the tab does not contain (the same rule ``_TabGuard`` keeps).
    """
    technicians = fetch_technicians(client)
    technicians_grid = build_technician_grid(technicians)
    # The header row is not data — a tab with only a header is zero rows.
    row_count = max(len(technicians_grid) - 1, 0)
    check_blank_columns("technicians", technicians_grid)
    if not dry_run:
        export_store.replace_grid("technicians", technicians_grid)
    # Recorded only after the tab is written, for the same reason as `jobs`: a
    # `_meta` row must never describe a tab that isn't there.
    new_meta_rows.add(
        MetaRow(
            feed="technicians",
            last_run_at=run_at,
            last_cursor="",
            row_count=row_count,
            exporter_version=EXPORTER_VERSION,
            contract_version=TECHNICIANS_CONTRACT_VERSION,
        )
    )
    return row_count


class _TabGuard:
    """Per-tab isolation for a feed that writes several independent tabs.

    One tab failing must cost exactly that tab. A guarded tab that succeeds is
    written and gets a fresh `_meta` row; one that fails is NOT written — its
    previous contents stay exactly as they were and its previous `_meta` row is
    carried forward unchanged, so ``last_run_at`` still says when that tab was
    last genuinely refreshed rather than claiming a run that produced nothing.

    Only ``STCLIError`` is caught — which, since ``st_cli.client`` wraps httpx
    transport failures in ``TransportError``, really is every way ServiceTitan
    can let us down, plus every ``ReportUnavailableError``. A ``KeyError`` or
    ``TypeError`` out of the row-mapping code is a bug in this repo, not a
    tenant's bad day, and must still crash loudly rather than quietly emptying a
    tab.

    ``row_count`` is derived from the GRID, never from the fetched record list:
    the builders drop keyless records, so counting the records would report rows
    that were never written.

    A **403** is not a tab failure and is handed to the ``ScopeLedger`` instead:
    it means either "this entity was never granted" or "this permission was
    revoked". It is classified for THAT TAB and costs that tab only — ServiceTitan
    grants per entity, so `Pricebook -> Materials` is a separate tick-box from
    `-> Services`, `-> Equipment` and `-> Categories`, and a TrueQuote-only tenant
    is refused Materials on every run while reading the other three perfectly
    well. The feed's remaining tabs are still attempted. See ``scopes.py``. Every
    other ``STCLIError`` keeps the per-tab behaviour above, unchanged.
    """

    def __init__(
        self,
        *,
        label: str,
        contract_version: str,
        meta_rows: dict[str, MetaRow],
        new_meta_rows: MetaRowSet,
        run_at: str,
        scopes: ScopeLedger | None = None,
    ) -> None:
        # The FEED name (`pricebook`, `financial`), used for logging only. The
        # ScopeLedger keys on the TAB, because that is how ServiceTitan grants.
        self._label = label
        self._contract_version = contract_version
        self._meta_rows = meta_rows
        self._new_meta_rows = new_meta_rows
        self._run_at = run_at
        self._scopes = scopes
        self.grids: dict[str, list[list[str]]] = {}
        self.row_counts: dict[str, int] = {}
        self.failures: dict[str, str] = {}

    def attempt(self, tab_name: str, build: Any) -> Any:
        """Build one tab's grid behind the guard. Returns whatever ``build`` did.

        Every tab is attempted, always. A sibling's 403 never short-circuits this
        one: a permission is per entity, so `pricebook.materials` being refused
        says nothing at all about `pricebook.categories`, and skipping the rest of
        the feed to save four requests is what cost a TrueQuote-only tenant the
        categories tab it had paid for.
        """
        try:
            grid, carried = build()
        except STCLIError as exc:
            if self._scopes is not None and self._scopes.deny(tab_name, exc):
                # A 403: not this tab's failure but this tab's permission. The
                # ledger has already carried THIS tab's `_meta` row forward and
                # decided whether it is loud or quiet. The rest of the feed runs.
                return None
            self.failures[tab_name] = str(exc)
            logger.warning(
                "%s: %s was NOT written this run (%s). Its previous contents "
                "and _meta row are unchanged; the other tabs are unaffected.",
                self._label,
                tab_name,
                exc,
            )
            if tab_name in self._meta_rows:
                self._new_meta_rows.carry(self._meta_rows[tab_name])
            return None
        self.grids[tab_name] = grid
        check_blank_columns(tab_name, grid)
        # The header row is not data — a tab with only a header is zero rows.
        self.row_counts[tab_name] = max(len(grid) - 1, 0)
        self._new_meta_rows.add(
            MetaRow(
                feed=tab_name,
                last_run_at=self._run_at,
                # Full replace, re-derived every run: nothing to carry forward,
                # so no cursor.
                last_cursor="",
                row_count=self.row_counts[tab_name],
                exporter_version=EXPORTER_VERSION,
                contract_version=self._contract_version,
            )
        )
        return carried

    def write(self, export_store: SheetsPort, *, dry_run: bool) -> None:
        if dry_run:
            return
        for tab_name, grid in self.grids.items():
            export_store.replace_grid(tab_name, grid)


def _run_pricebook_feed(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    *,
    meta_rows: dict[str, MetaRow],
    new_meta_rows: MetaRowSet,
    run_at: str,
    category_ids: tuple[str, ...] = (),
    scopes: ScopeLedger | None = None,
    dry_run: bool,
) -> tuple[dict[str, int], dict[str, str], list[dict[str, Any]], bool]:
    """Write the four `pricebook.*` tabs; append one MetaRow per tab that succeeded.

    Returns the item records it fetched along with the row counts and failures:
    the image pass consumes them, but runs in ``_run`` *after* the `_meta` write
    rather than here, so no side lane can ever precede `_meta`.

    A full replace every run, with no window and no cursor: pricebook is a
    catalogue, so re-deriving every row from a fresh full list is both the simplest
    correct thing and naturally bounded — `last_cursor` is therefore blank for all
    four feeds, exactly like `technicians`.

    The three item tabs are built from ONE code path (``build_item_grid``) because
    they share one column set; only the tab name differs.

    **The four tabs are independent**, behind the same ``_TabGuard`` the financial
    feed uses. ``equipment`` failing while ``services`` and ``categories`` were
    read perfectly well is a real ServiceTitan afternoon, and there is no
    atomicity to protect: the tabs are four separate full replaces that a
    consumer joins by id, and a tab left at last run's contents is a strictly
    better answer than four tabs left at last run's contents. The failed tab is
    named in the run summary.

    The fourth return value is ``catalogue_complete``: True iff all three ITEM
    tabs produced a grid. It is what the image pass needs and the only thing it
    needs — a missing item tab, whether it failed or was refused, means those
    items never reached ``item_records`` and their image keys must not be pruned
    as "gone". Categories carry no images, so they do not enter it.
    """
    guard = _TabGuard(
        label="pricebook",
        contract_version=CONTRACT_VERSION,
        meta_rows=meta_rows,
        new_meta_rows=new_meta_rows,
        run_at=run_at,
        scopes=scopes,
    )

    item_records: list[dict[str, Any]] = []

    def build_items(resource: str) -> tuple[list[list[str]], list[dict[str, Any]]]:
        records = fetch_pricebook_items(client, resource, category_ids=category_ids)
        return build_item_grid(records), records

    for tab_name, tab_resource in PRICEBOOK_TABS.items():
        fetched = guard.attempt(tab_name, lambda r=tab_resource: build_items(r))
        if fetched:
            item_records.extend(fetched)

    guard.attempt(
        PRICEBOOK_CATEGORIES_TAB,
        lambda: (build_category_grid(fetch_pricebook_categories(client)), None),
    )

    logger.info(
        "pricebook: %s",
        " ".join(f"{tab}={count}" for tab, count in sorted(guard.row_counts.items()))
        or "nothing written",
    )

    guard.write(export_store, dry_run=dry_run)

    catalogue_complete = all(tab_name in guard.grids for tab_name in PRICEBOOK_TABS)

    return guard.row_counts, guard.failures, item_records, catalogue_complete


def _run_financial_feed(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    *,
    meta_rows: dict[str, MetaRow],
    new_meta_rows: MetaRowSet,
    run_at: str,
    today: Any,
    window_days: int,
    max_jobs: int,
    scopes: ScopeLedger | None = None,
    dry_run: bool,
) -> tuple[dict[str, int], dict[str, str]]:
    """Write the four `financial` tabs; append one MetaRow per tab that succeeded.

    **The four tabs are independent, and that is the point.** ``reporting.jobCosts``
    depends on a report that may be absent, ambiguous or throttled, and
    ``payroll.timesheets`` costs one request per completed job — either can fail on
    a tenant where invoices and business units are perfectly readable. So each tab
    is built behind its own guard:

    ``_TabGuard`` (shared with the pricebook feed) is what makes that true — see
    its docstring for the carry-forward and the deliberately narrow ``except``.

    Unlike the pricebook feed this one is **window-bounded** — invoices and
    timesheets grow without limit. See ``window.FINANCIAL_WINDOW_DAYS``.
    """
    guard = _TabGuard(
        label="financial",
        # Window-bounded full replace, re-derived every run exactly like the jobs
        # tab: the window is time-relative, so a row's membership has to be
        # re-decided each run regardless of what changed.
        contract_version=FINANCIAL_CONTRACT_VERSION,
        meta_rows=meta_rows,
        new_meta_rows=new_meta_rows,
        run_at=run_at,
        scopes=scopes,
    )

    guard.attempt(
        FINANCIAL_INVOICES_TAB,
        lambda: (
            build_invoice_grid(fetch_invoices(client, today=today, window_days=window_days)),
            None,
        ),
    )
    guard.attempt(
        FINANCIAL_TIMESHEETS_TAB,
        lambda: (
            build_timesheet_grid(
                fetch_timesheets(
                    client,
                    fetch_completed_job_ids(
                        client, today=today, window_days=window_days, max_jobs=max_jobs
                    ),
                )
            ),
            None,
        ),
    )
    guard.attempt(
        FINANCIAL_BUSINESS_UNITS_TAB,
        lambda: (build_business_unit_grid(fetch_business_unit_list(client)), None),
    )
    guard.attempt(
        FINANCIAL_JOB_COSTS_TAB,
        lambda: (
            build_job_cost_grid(fetch_job_costs(client, today=today, window_days=window_days)),
            None,
        ),
    )

    logger.info(
        "financial (window=%dd): %s",
        window_days,
        " ".join(f"{tab}={count}" for tab, count in sorted(guard.row_counts.items()))
        or "nothing written",
    )

    guard.write(export_store, dry_run=dry_run)

    return guard.row_counts, guard.failures


def _upload_pricebook_images(
    client: ServiceTitanClient,
    raw_cache_store: SheetsPort,
    item_records: list[dict[str, Any]],
    *,
    image_client: TrueQuoteImageClient | None,
    image_deadline: float | None,
    run_at: str,
    dry_run: bool,
    catalogue_complete: bool = True,
) -> ImageUploadSummary | None:
    """Push image bytes to TrueQuote for the items this feed just exported.

    A flag on the pricebook feed rather than a feed of its own, because the
    assets it uploads are a column of the rows that were just fetched: running
    it separately would mean re-listing the whole catalogue to learn the same
    thing. It inherits the pricebook cadence for the same reason images change
    when the catalogue changes.

    Returns None — not an empty summary — when the pass did not run at all:
    "nothing to upload" and "never looked" are different facts to whoever reads
    the run's output.

    ``catalogue_complete`` is False when any pricebook item tab failed. The
    summary cannot know that — it only sees the records it was handed — but
    ``ImageLedger.keep``'s precondition is that the caller saw the WHOLE
    catalogue, and a failed `pricebook.equipment` means every equipment image
    key is simply absent from ``seen_keys`` rather than gone.

    ``image_deadline`` bounds the pass so it ends with a flushed ledger rather
    than a SIGKILL. A pass that runs out of budget is ``stopped``, which already
    vetoes the prune for exactly the right reason: it did not see the catalogue.
    """
    if image_client is None or dry_run:
        return None

    ledger = ImageLedger(raw_cache_store)
    summary = ImageUploadSummary()
    try:
        summary = upload_pricebook_images(
            client, image_client, ledger, item_records, now=run_at, deadline=image_deadline
        )
        # Only prune on a pass that actually saw the whole catalogue — a run
        # stopped by a 403, a rate limit or a failed download has "not looked
        # at" assets that must not be mistaken for "gone" and re-uploaded next
        # run. A failed ITEM TAB is the same fact one level up: its items never
        # reached `item_records`, so `catalogue_complete` vetoes the prune too.
        if summary.complete and catalogue_complete:
            ledger.keep(summary.seen_keys)
    except Exception as exc:
        # The image lane is a SIDE lane. Every pricebook tab AND its `_meta` row
        # are already written by the time this runs (see the call site in
        # `_run`), so nothing escaping here can leave a fresh tab described by a
        # stale `_meta` row or skip the outbox drain. Whatever it is, it is
        # logged with its traceback, named in the summary, and does not decide
        # whether the export succeeded.
        summary.stopped = f"image pass aborted: {type(exc).__name__}: {exc}"
        logger.exception(
            "pricebook image pass aborted (%s). Every pricebook tab and its _meta row "
            "were still written; the images are retried next run.",
            exc,
        )
    finally:
        # In a `finally` because the uploads this pass DID make are recorded in
        # this ledger and nowhere else: losing it re-sends bytes TrueQuote
        # already has, every run, forever.
        #
        # In its OWN try/except because the flush is a Sheets write to the
        # raw-cache spreadsheet, and a 429 there is routine. An exception from a
        # `finally` replaces whatever the body was doing and walks straight past
        # the `except` above — so guarding the body alone left this lane able to
        # abort the run after all. Losing the ledger costs re-uploaded bytes;
        # that is a named `stopped`, not a failed export.
        try:
            ledger.flush()
        except Exception as exc:  # noqa: BLE001 - a side lane may not end the run
            summary.stopped = f"image ledger flush failed: {type(exc).__name__}: {exc}"
            logger.exception(
                "pricebook image ledger flush failed (%s). Every pricebook tab and its "
                "_meta row were still written; the uploads this pass made are not "
                "recorded, so their bytes are re-sent next run.",
                exc,
            )

    logger.info("pricebook images: %s", summary.as_log_fields())
    if summary.permission_denied:
        logger.warning(
            "pricebook image upload incomplete: the tenant has not granted "
            "`Pricebook -> Images`. Every pricebook tab was still written."
        )
    return summary


def _apply_window(
    rows: list[dict[str, Any]],
    *,
    today: Any,
    window_days: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Split denormalised rows into (kept, skipped_no_start, skipped_bad_timestamp).

    ``in_window`` itself treats "out of window" and "unparsable" identically (both
    return ``False``, deliberately — see its docstring) so one bad record can't
    crash the run. This function is what recovers the distinction for logging: a
    genuinely out-of-window row is expected and not counted; a missing or
    unparsable ``appointment_start`` is unusual and worth surfacing separately.
    """
    kept: list[dict[str, Any]] = []
    skipped_no_start = 0
    skipped_bad_timestamp = 0
    for row in rows:
        start = row["appointment_start"]
        if not start:
            skipped_no_start += 1
            continue
        if not _is_parseable_timestamp(start):
            skipped_bad_timestamp += 1
            continue
        if in_window(start, today=today, window_days=window_days):
            kept.append(row)
    return kept, skipped_no_start, skipped_bad_timestamp


def _is_parseable_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    return True


def _prune_raw_caches(
    denormalized_rows: list[dict[str, Any]],
    windowed_rows: list[dict[str, Any]],
    *,
    raw_appointments: RawCache,
    raw_assignments: RawCache,
) -> None:
    """Bound the two fastest-growing raw caches — appointments and assignment
    events — instead of letting them accumulate every record ever seen forever.

    Only prunes an appointment that successfully joined a (non-cancelled) job
    (i.e. appears in ``denormalized_rows``) but was excluded purely by the window/
    timestamp check (i.e. is absent from ``windowed_rows``). That's the only
    provably safe boundary: ServiceTitan resends an appointment's own record
    whenever the appointment itself mutates — including a reschedule — so if a
    pruned appointment later moves back into the window, its own delta brings it
    back via ``RawCache.merge``.

    Deliberately does NOT prune ``raw_jobs``/``raw_customers``/``raw_locations``,
    and does NOT prune an appointment that hasn't found its job yet (absent from
    ``denormalized_rows`` entirely) or whose job is currently cancelled (also
    absent from ``denormalized_rows``, per denormalize.py's status filter) —
    both must stay cached indefinitely so they can rejoin once their job arrives
    or is un-cancelled. This was verified the hard way: an earlier version of
    this function pruned jobs by "not referenced by a currently-windowed
    appointment" and broke the exact "appointment rescheduled from outside the
    window into today" scenario — job 2 was pruned after run 1 (its only
    appointment was out-of-window), and since a job's own record is only resent
    when the JOB itself changes (not when a dependent appointment reschedules),
    job 2 never came back, and the rescheduled appointment could no longer be
    denormalized. Pruning jobs/customers/locations at all requires knowing no
    appointment will ever reference them again, which nothing in this design can
    guarantee — so they're left unpruned rather than risk that class of bug.
    """
    denormalized_appointment_ids = {str(row["st_appointment_id"]) for row in denormalized_rows}
    windowed_appointment_ids = {str(row["st_appointment_id"]) for row in windowed_rows}
    safely_prunable_ids = denormalized_appointment_ids - windowed_appointment_ids

    retained_appointment_ids = set(raw_appointments.records) - safely_prunable_ids
    raw_appointments.keep(retained_appointment_ids)

    retained_assignment_ids = {
        str(a["id"])
        for a in raw_assignments.values()
        if "id" in a and str(a.get("appointmentId")) in retained_appointment_ids
    }
    raw_assignments.keep(retained_assignment_ids)

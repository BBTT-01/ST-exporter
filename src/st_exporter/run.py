"""Orchestrates one export run: fetch deltas, merge, denormalise, window, write.

See ``denormalize.py`` and ``meta.py`` for why the fetch is incremental (cursor per
feed) while the write is a full, freshly re-derived replace every run — the window
predicate is time-relative, so a row's membership must be re-decided every run
regardless of whether its underlying record changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from st_cli.client import ServiceTitanClient
from st_cli.config import Settings
from st_cli.exceptions import ConfigError, STCLIError
from st_exporter import EXPORTER_VERSION
from st_exporter.config import ExporterSettings
from st_exporter.denormalize import build_job_rows
from st_exporter.feeds.appointments import fetch_appointments_delta
from st_exporter.feeds.assignments import fetch_assignments_delta
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
    JOB_COLUMNS,
    TECHNICIAN_COLUMNS,
    format_job_row,
    format_technician_row,
)
from st_exporter.images.client import TrueQuoteImageClient
from st_exporter.images.ledger import ImageLedger
from st_exporter.images.upload import ImageUploadSummary, upload_pricebook_images
from st_exporter.logging_setup import configure_logging, logger
from st_exporter.meta import CursorBundle, MetaRow, build_meta_grid, parse_meta_grid
from st_exporter.pricebook import CONTRACT_VERSION, build_category_grid, build_item_grid
from st_exporter.sheets import SheetsClient, SheetsPort, get_gspread_client
from st_exporter.window import DEFAULT_WINDOW_DAYS, FINANCIAL_WINDOW_DAYS, in_window

_RAW_CUSTOMERS = "_raw_customers"
_RAW_LOCATIONS = "_raw_locations"
_RAW_JOBS = "_raw_jobs"
_RAW_APPOINTMENTS = "_raw_appointments"
_RAW_ASSIGNMENTS = "_raw_assignments"

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
    # selected this run. None and {} are different: {} would mean "ran, wrote
    # nothing", which never happens (a tab always gets at least its header).
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
            pricebook_category_ids=exporter_settings.pricebook_category_ids,
            image_client=image_client,
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
    pricebook_category_ids: tuple[str, ...] = (),
    image_client: TrueQuoteImageClient | None = None,
    dry_run: bool,
) -> ExportSummary:
    now = datetime.now(timezone.utc)
    today = now.date()
    run_at = now.isoformat()

    meta_rows = parse_meta_grid(export_store.read_grid("_meta"))
    new_meta_rows: list[MetaRow] = []

    windowed_rows: list[dict[str, Any]] = []
    skipped_no_job = 0
    jobs_row_count = meta_rows["jobs"].row_count if "jobs" in meta_rows else 0

    if "jobs" in feeds:
        windowed_rows, skipped_no_job = _run_jobs_feed(
            client,
            export_store,
            raw_cache_store,
            meta_rows=meta_rows,
            new_meta_rows=new_meta_rows,
            today=today,
            run_at=run_at,
            window_days=window_days,
            dry_run=dry_run,
        )
        jobs_row_count = len(windowed_rows)
    elif "jobs" in meta_rows:
        new_meta_rows.append(meta_rows["jobs"])

    technician_rows: list[dict[str, Any]] = []
    technicians_row_count = meta_rows["technicians"].row_count if "technicians" in meta_rows else 0

    if "technicians" in feeds:
        technician_rows = _run_technicians_feed(
            client,
            export_store,
            new_meta_rows=new_meta_rows,
            run_at=run_at,
            dry_run=dry_run,
        )
        technicians_row_count = len(technician_rows)
    elif "technicians" in meta_rows:
        new_meta_rows.append(meta_rows["technicians"])

    pricebook_row_counts: dict[str, int] | None = None
    pricebook_failures: dict[str, str] | None = None
    image_summary: ImageUploadSummary | None = None
    pricebook_item_records: list[dict[str, Any]] | None = None
    if "pricebook" in feeds:
        pricebook_row_counts, pricebook_failures, pricebook_item_records = _run_pricebook_feed(
            client,
            export_store,
            meta_rows=meta_rows,
            new_meta_rows=new_meta_rows,
            run_at=run_at,
            category_ids=pricebook_category_ids,
            dry_run=dry_run,
        )
    else:
        for feed_name in PRICEBOOK_FEED_NAMES:
            if feed_name in meta_rows:
                new_meta_rows.append(meta_rows[feed_name])

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
            dry_run=dry_run,
        )
    else:
        for feed_name in FINANCIAL_FEED_NAMES:
            if feed_name in meta_rows:
                new_meta_rows.append(meta_rows[feed_name])

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
        image_summary = _upload_pricebook_images(
            client,
            raw_cache_store,
            pricebook_item_records,
            image_client=image_client,
            run_at=run_at,
            dry_run=dry_run,
            # A failed item tab means `item_records` is missing that tab's items,
            # so this pass did NOT see the whole catalogue however well it ran.
            catalogue_complete=not pricebook_failures,
        )

    return ExportSummary(
        jobs_row_count=jobs_row_count,
        technicians_row_count=technicians_row_count,
        skipped_no_job=skipped_no_job,
        dry_run=dry_run,
        pricebook_row_counts=pricebook_row_counts,
        pricebook_failures=pricebook_failures,
        images=image_summary,
        financial_row_counts=financial_row_counts,
        financial_failures=financial_failures,
    )


def _run_jobs_feed(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    raw_cache_store: SheetsPort,
    *,
    meta_rows: dict[str, MetaRow],
    new_meta_rows: list[MetaRow],
    today: Any,
    run_at: str,
    window_days: int,
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

    job_types = fetch_job_types(client)
    business_units = fetch_business_units(client)

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

    jobs_grid = [list(JOB_COLUMNS)] + [format_job_row(row) for row in windowed_rows]

    new_cursor_bundle = CursorBundle(
        {
            "customers": customers_cursor,
            "locations": locations_cursor,
            "jobs": jobs_cursor,
            "appointments": appointments_cursor,
            "assignments": assignments_cursor,
        }
    )
    new_meta_rows.append(
        MetaRow(
            feed="jobs",
            last_run_at=run_at,
            last_cursor=new_cursor_bundle.encode(),
            row_count=len(windowed_rows),
            exporter_version=EXPORTER_VERSION,
        )
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

    return windowed_rows, denormalized.skipped_no_job


def _run_technicians_feed(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    *,
    new_meta_rows: list[MetaRow],
    run_at: str,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Fetch technicians and (unless dry-run) write the tab; append its MetaRow."""
    technicians = fetch_technicians(client)
    technician_rows = _dedupe_technician_rows([_technician_row(record) for record in technicians])
    new_meta_rows.append(
        MetaRow(
            feed="technicians",
            last_run_at=run_at,
            last_cursor="",
            row_count=len(technician_rows),
            exporter_version=EXPORTER_VERSION,
        )
    )
    if not dry_run:
        technicians_grid = [list(TECHNICIAN_COLUMNS)] + [
            format_technician_row(row) for row in technician_rows
        ]
        export_store.replace_grid("technicians", technicians_grid)
    return technician_rows


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
    """

    def __init__(
        self,
        *,
        label: str,
        contract_version: str,
        meta_rows: dict[str, MetaRow],
        new_meta_rows: list[MetaRow],
        run_at: str,
    ) -> None:
        self._label = label
        self._contract_version = contract_version
        self._meta_rows = meta_rows
        self._new_meta_rows = new_meta_rows
        self._run_at = run_at
        self.grids: dict[str, list[list[str]]] = {}
        self.row_counts: dict[str, int] = {}
        self.failures: dict[str, str] = {}

    def attempt(self, tab_name: str, build: Any) -> Any:
        """Build one tab's grid behind the guard. Returns whatever ``build`` did."""
        try:
            grid, carried = build()
        except STCLIError as exc:
            self.failures[tab_name] = str(exc)
            logger.warning(
                "%s: %s was NOT written this run (%s). Its previous contents "
                "and _meta row are unchanged; the other tabs are unaffected.",
                self._label,
                tab_name,
                exc,
            )
            if tab_name in self._meta_rows:
                self._new_meta_rows.append(self._meta_rows[tab_name])
            return None
        self.grids[tab_name] = grid
        # The header row is not data — a tab with only a header is zero rows.
        self.row_counts[tab_name] = max(len(grid) - 1, 0)
        self._new_meta_rows.append(
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
    new_meta_rows: list[MetaRow],
    run_at: str,
    category_ids: tuple[str, ...] = (),
    dry_run: bool,
) -> tuple[dict[str, int], dict[str, str], list[dict[str, Any]]]:
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
    """
    guard = _TabGuard(
        label="pricebook",
        contract_version=CONTRACT_VERSION,
        meta_rows=meta_rows,
        new_meta_rows=new_meta_rows,
        run_at=run_at,
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

    return guard.row_counts, guard.failures, item_records


def _run_financial_feed(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    *,
    meta_rows: dict[str, MetaRow],
    new_meta_rows: list[MetaRow],
    run_at: str,
    today: Any,
    window_days: int,
    max_jobs: int,
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
    """
    if image_client is None or dry_run:
        return None

    ledger = ImageLedger(raw_cache_store)
    summary = ImageUploadSummary()
    try:
        summary = upload_pricebook_images(client, image_client, ledger, item_records, now=run_at)
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


def _dedupe_technician_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeats of the same technician, keeping the first occurrence.

    The dedupe key is ``st_technician_id`` — the tab's own identity, and the key
    the `jobs` tab's ``st_technician_id`` column joins against. A second row for
    an id already emitted carries no information the first doesn't, so dropping
    it is always safe; it also makes the tab robust against the list endpoint
    returning a record twice across page boundaries.

    It is deliberately NOT ``email``. Two DISTINCT technician ids sharing one
    address is a real ServiceTitan state (a re-created technician record, or a
    shop's shared inbox on several techs), and both of those technicians can be
    assigned to jobs — so both must appear here or a `jobs` row would reference a
    technician missing from the tab. A downstream unique-email constraint is the
    downstream's to resolve; we log the collision rather than silently deleting a
    real technician to satisfy it. Rows with no id are all kept, for the same
    "never drop a real technician" reason.
    """
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    duplicate_id_count = 0
    for row in rows:
        technician_id = row.get("st_technician_id")
        if technician_id is not None:
            key = str(technician_id)
            if key in seen_ids:
                duplicate_id_count += 1
                continue
            seen_ids.add(key)
        deduped.append(row)

    if duplicate_id_count:
        logger.warning(
            "technicians: dropped %d duplicate row(s) for an already-exported st_technician_id",
            duplicate_id_count,
        )

    ids_by_email: dict[str, list[str]] = {}
    for row in deduped:
        email = str(row.get("email") or "").strip().lower()
        if not email:
            continue
        ids_by_email.setdefault(email, []).append(str(row.get("st_technician_id")))
    for email, ids in ids_by_email.items():
        if len(ids) > 1:
            logger.warning(
                "technicians: %s is shared by %d distinct technician ids (%s); all "
                "are exported — a consumer requiring unique emails must resolve it",
                email,
                len(ids),
                ", ".join(ids),
            )

    return deduped


def _technician_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "st_technician_id": record.get("id"),
        "name": record.get("name"),
        "email": record.get("email"),
        "active": record.get("active"),
    }


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

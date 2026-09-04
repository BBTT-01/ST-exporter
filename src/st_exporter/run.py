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
from st_cli.exceptions import ConfigError
from st_exporter import EXPORTER_VERSION
from st_exporter.config import ExporterSettings
from st_exporter.denormalize import build_job_rows
from st_exporter.feeds.appointments import fetch_appointments_delta
from st_exporter.feeds.assignments import fetch_assignments_delta
from st_exporter.feeds.customers import fetch_customers_delta
from st_exporter.feeds.jobs import fetch_jobs_delta
from st_exporter.feeds.locations import fetch_locations_delta
from st_exporter.feeds.raw_cache import RawCache
from st_exporter.feeds.reference import fetch_business_units, fetch_job_types, fetch_technicians
from st_exporter.format import (
    JOB_COLUMNS,
    TECHNICIAN_COLUMNS,
    format_job_row,
    format_technician_row,
)
from st_exporter.logging_setup import configure_logging, logger
from st_exporter.meta import CursorBundle, MetaRow, build_meta_grid, parse_meta_grid
from st_exporter.sheets import SheetsClient, SheetsPort, get_gspread_client
from st_exporter.window import DEFAULT_WINDOW_DAYS, in_window

_RAW_CUSTOMERS = "_raw_customers"
_RAW_LOCATIONS = "_raw_locations"
_RAW_JOBS = "_raw_jobs"
_RAW_APPOINTMENTS = "_raw_appointments"
_RAW_ASSIGNMENTS = "_raw_assignments"

_VALID_FEEDS = frozenset({"jobs", "technicians"})
DEFAULT_FEEDS = frozenset({"jobs", "technicians"})


def parse_feeds(value: str) -> frozenset[str]:
    """Parse a comma-separated --feeds value into a validated set.

    Blank segments are dropped so "jobs," or " jobs , technicians " both work —
    the reusable workflow's `feeds` input is free-text, not a strict enum.
    """
    feeds = frozenset(part.strip() for part in value.split(",") if part.strip())
    if not feeds:
        raise ConfigError("--feeds must name at least one of: jobs, technicians.")
    unknown = feeds - _VALID_FEEDS
    if unknown:
        raise ConfigError(
            f"unknown feed(s): {', '.join(sorted(unknown))}. Valid feeds: jobs, technicians."
        )
    return feeds


@dataclass
class ExportSummary:
    jobs_row_count: int
    technicians_row_count: int
    skipped_no_job: int
    dry_run: bool


def run_export(
    st_settings: Settings,
    exporter_settings: ExporterSettings,
    *,
    feeds: frozenset[str] = DEFAULT_FEEDS,
    pricebook: bool = False,
    dry_run: bool = False,
    client: ServiceTitanClient | None = None,
    export_store: SheetsPort | None = None,
    raw_cache_store: SheetsPort | None = None,
) -> ExportSummary:
    """Run one export: fetch feeds, denormalise, window-filter, write the Sheets.

    ``feeds`` selects which output tab(s) this call fetches and writes — either
    subset lets the reusable workflow honor the spec's different cadences (jobs
    ~5 min, technicians ~30 min) without re-fetching the unneeded feed every
    time. The feed not selected is left byte-for-byte untouched in both the
    Export Store's tab and its `_meta` row.

    ``client``/``export_store``/``raw_cache_store`` can be injected (used by tests
    with fixtures and an in-memory Sheets double); left as ``None`` in production,
    where real ones are constructed from ``st_settings``/``exporter_settings``.
    ``pricebook`` is accepted and does nothing when set, per ticket 05's explicit
    scope cut — the CLI has no price-book commands to call anyway.
    """
    configure_logging()
    if pricebook:
        logger.info("pricebook=true has no effect (out of scope for this exporter)")

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

    if dry_run:
        logger.info(
            "dry-run: would write jobs=%d technicians=%d (nothing written)",
            jobs_row_count,
            technicians_row_count,
        )
    else:
        export_store.replace_grid("_meta", build_meta_grid(new_meta_rows))

    return ExportSummary(
        jobs_row_count=jobs_row_count,
        technicians_row_count=technicians_row_count,
        skipped_no_job=skipped_no_job,
        dry_run=dry_run,
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
    technician_rows = [_technician_row(record) for record in technicians]
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

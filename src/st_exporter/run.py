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
from st_exporter.sheets import InMemorySheetsStore, SheetsClient, SheetsPort, get_gspread_client
from st_exporter.window import DEFAULT_WINDOW_DAYS, in_window

_RAW_CUSTOMERS = "_raw_customers"
_RAW_LOCATIONS = "_raw_locations"
_RAW_JOBS = "_raw_jobs"
_RAW_APPOINTMENTS = "_raw_appointments"
_RAW_ASSIGNMENTS = "_raw_assignments"


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
    pricebook: bool = False,
    dry_run: bool = False,
    client: ServiceTitanClient | None = None,
    export_store: SheetsPort | None = None,
    raw_cache_store: SheetsPort | None = None,
) -> ExportSummary:
    """Run one export: fetch feeds, denormalise, window-filter, write the Sheets.

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
    if dry_run:
        # No real Sheets access in dry-run mode: always behaves like a first run.
        return (export_store or InMemorySheetsStore()), (raw_cache_store or InMemorySheetsStore())
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
    window_days: int = DEFAULT_WINDOW_DAYS,
    dry_run: bool,
) -> ExportSummary:
    now = datetime.now(timezone.utc)
    today = now.date()

    meta_rows = parse_meta_grid(export_store.read_grid("_meta"))
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
    technicians = fetch_technicians(client)

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

    windowed_rows = [
        row
        for row in denormalized.rows
        if row["appointment_start"]
        and in_window(row["appointment_start"], today=today, window_days=window_days)
    ]

    jobs_grid = [list(JOB_COLUMNS)] + [format_job_row(row) for row in windowed_rows]
    technician_rows = [_technician_row(record) for record in technicians]
    technicians_grid = [list(TECHNICIAN_COLUMNS)] + [
        format_technician_row(row) for row in technician_rows
    ]

    new_cursor_bundle = CursorBundle(
        {
            "customers": customers_cursor,
            "locations": locations_cursor,
            "jobs": jobs_cursor,
            "appointments": appointments_cursor,
            "assignments": assignments_cursor,
        }
    )
    run_at = now.isoformat()
    new_meta_rows = [
        MetaRow(
            feed="jobs",
            last_run_at=run_at,
            last_cursor=new_cursor_bundle.encode(),
            row_count=len(windowed_rows),
            exporter_version=EXPORTER_VERSION,
        ),
        MetaRow(
            feed="technicians",
            last_run_at=run_at,
            last_cursor="",
            row_count=len(technician_rows),
            exporter_version=EXPORTER_VERSION,
        ),
    ]

    if dry_run:
        logger.info(
            "dry-run: would write jobs=%d technicians=%d (nothing written)",
            len(windowed_rows),
            len(technician_rows),
        )
    else:
        raw_cache_store.replace_grid(_RAW_CUSTOMERS, raw_customers.to_grid())
        raw_cache_store.replace_grid(_RAW_LOCATIONS, raw_locations.to_grid())
        raw_cache_store.replace_grid(_RAW_JOBS, raw_jobs.to_grid())
        raw_cache_store.replace_grid(_RAW_APPOINTMENTS, raw_appointments.to_grid())
        raw_cache_store.replace_grid(_RAW_ASSIGNMENTS, raw_assignments.to_grid())

        export_store.replace_grid("jobs", jobs_grid)
        export_store.replace_grid("technicians", technicians_grid)
        export_store.replace_grid("_meta", build_meta_grid(new_meta_rows))

    return ExportSummary(
        jobs_row_count=len(windowed_rows),
        technicians_row_count=len(technician_rows),
        skipped_no_job=denormalized.skipped_no_job,
        dry_run=dry_run,
    )


def _technician_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "st_technician_id": record.get("id"),
        "name": record.get("name"),
        "email": record.get("email"),
        "active": record.get("active"),
    }

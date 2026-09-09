"""End-to-end test of run.py's orchestration against fixture ServiceTitan data.

Runs the exporter twice (respx-mocked HTTP, an in-memory Sheets double standing in
for gspread) to prove: incremental fetch only pulls deltas on the second run
(enforced by ``tenant_run2``'s cursor assertions), a rescheduled-into-window
appointment appears once its delta lands, an appointment untouched by that delta
formats to a byte-identical row across both runs, and no secret value ever
surfaces in captured log output — even with logging forced to DEBUG.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import respx

from st_exporter.meta import CursorBundle, parse_meta_grid
from st_exporter.run import _apply_window, run_export
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token
from tests.st_exporter.fixtures import tenant_run1, tenant_run2

FIXED_TODAY = date(2026, 9, 3)
FIXED_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _frozen_now():
    return patch("st_exporter.run.datetime", **{"now.return_value": FIXED_NOW})


@respx.mock
def test_two_runs_incremental_fetch_and_byte_identical_unchanged_rows(
    st_settings, exporter_settings
) -> None:
    api_base = st_settings.api_base
    today_iso = FIXED_TODAY.isoformat()
    far_past_iso = (FIXED_TODAY - timedelta(days=200)).isoformat()

    export_store = InMemorySheetsStore()
    raw_cache_store = InMemorySheetsStore()

    mock_auth_token(st_settings.auth_url)

    # --- Run 1: empty raw cache, full drain from each feed's beginning. ---
    tenant_run1.register(api_base, today_iso=today_iso, far_past_iso=far_past_iso)
    with _frozen_now():
        summary1 = run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )

    # Job 1's appointment is today (in-window). Job 2's is 200 days ago
    # (outside the 90-day window) and must NOT appear yet.
    assert summary1.jobs_row_count == 1
    jobs_grid_1 = export_store.tabs["jobs"]
    assert {row[0] for row in jobs_grid_1[1:]} == {"1"}

    # --- Run 2: only tenant_run2's routes exist now — any request that doesn't
    # carry exactly run 1's cursor fails inside the fixture's own assertion,
    # which is the delta-only-fetch proof (no separate call-count bookkeeping
    # needed here). ---
    tenant_run2.register(api_base, today_iso=today_iso)
    with _frozen_now():
        summary2 = run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )

    # Job 2's appointment was rescheduled into the window -> both jobs now present.
    assert summary2.jobs_row_count == 2
    jobs_grid_2 = export_store.tabs["jobs"]
    assert {row[0] for row in jobs_grid_2[1:]} == {"1", "2"}

    # Job 1's row, untouched by run 2's delta, is byte-identical across both runs.
    row1_run1 = next(row for row in jobs_grid_1[1:] if row[0] == "1")
    row1_run2 = next(row for row in jobs_grid_2[1:] if row[0] == "1")
    assert row1_run1 == row1_run2

    # _meta's cursor decodes to run 2's tokens, not run 1's or a mix.
    meta = parse_meta_grid(export_store.tabs["_meta"])
    bundle = CursorBundle.decode(meta["jobs"].last_cursor)
    assert bundle.get("jobs") == tenant_run2.JOBS_CURSOR
    assert bundle.get("appointments") == tenant_run2.APPOINTMENTS_CURSOR
    assert bundle.get("customers") == tenant_run2.CUSTOMERS_CURSOR
    assert meta["jobs"].row_count == 2


@respx.mock
def test_dry_run_reads_real_state_and_writes_nothing(st_settings, exporter_settings) -> None:
    api_base = st_settings.api_base
    today_iso = FIXED_TODAY.isoformat()
    far_past_iso = (FIXED_TODAY - timedelta(days=200)).isoformat()

    export_store = InMemorySheetsStore()
    raw_cache_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)

    # Run 1, for real, to populate cursor/raw-cache state to preview against.
    tenant_run1.register(api_base, today_iso=today_iso, far_past_iso=far_past_iso)
    with _frozen_now():
        run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )
    snapshot = {name: [row[:] for row in grid] for name, grid in export_store.tabs.items()}
    raw_snapshot = {name: [row[:] for row in grid] for name, grid in raw_cache_store.tabs.items()}

    # Dry run against the SAME real stores. tenant_run2's routes only accept
    # exactly run 1's cursor (asserted inside the fixture itself) — if dry-run
    # had reset to first-run/empty-cache behavior instead of reading real state,
    # this would fail with a cursor mismatch, not just a wrong count.
    tenant_run2.register(api_base, today_iso=today_iso)
    with _frozen_now():
        summary = run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=raw_cache_store,
            dry_run=True,
        )

    # Job 2's appointment was rescheduled into the window in tenant_run2 — an
    # accurate preview of the next real run reports both jobs, not a first-run 1.
    assert summary.jobs_row_count == 2
    assert summary.dry_run is True

    # Nothing was written: both stores are byte-identical to the pre-dry-run snapshot.
    assert export_store.tabs == snapshot
    assert raw_cache_store.tabs == raw_snapshot


def test_apply_window_counts_missing_start_separately_from_out_of_window() -> None:
    # Distinct from a genuinely out-of-window row (expected, not counted) and
    # from an unparsable one (window.in_window's own concern) — a live
    # appointment with literally no start value is unusual enough to surface.
    rows = [
        {"appointment_start": "2026-09-03T09:00:00-05:00"},  # in window
        {"appointment_start": ""},  # missing
        {"appointment_start": None},  # missing
        {"appointment_start": "2020-01-01T00:00:00-05:00"},  # genuinely out of window
    ]
    kept, skipped_no_start, skipped_bad_timestamp = _apply_window(
        rows, today=FIXED_TODAY, window_days=90
    )
    assert len(kept) == 1
    assert skipped_no_start == 2
    assert skipped_bad_timestamp == 0


def test_apply_window_counts_bad_timestamp_separately_from_missing() -> None:
    rows = [{"appointment_start": "not-a-real-timestamp"}]
    kept, skipped_no_start, skipped_bad_timestamp = _apply_window(
        rows, today=FIXED_TODAY, window_days=90
    )
    assert kept == []
    assert skipped_no_start == 0
    assert skipped_bad_timestamp == 1


class _ListHandler(logging.Handler):
    """Captures records directly off a specific logger.

    Not pytest's ``caplog`` — since ``configure_logging()`` (D1) deliberately sets
    ``st_exporter``'s logger to ``propagate = False`` (so it can't double up with
    root handlers a hosting environment might add), caplog's root-attached
    handler never sees its records, even via ``caplog.set_level(logger=...)``
    (verified: that call only adjusts the named logger's level, it does not
    attach caplog's handler to it). This is what actually lets that INFO-level
    output be inspected.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records_captured: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records_captured.append(record)


@respx.mock
def test_no_secret_leaks_even_with_root_logger_forced_to_debug(
    st_settings, exporter_settings
) -> None:
    today_iso = FIXED_TODAY.isoformat()
    far_past_iso = (FIXED_TODAY - timedelta(days=200)).isoformat()
    mock_auth_token(st_settings.auth_url)
    tenant_run1.register(st_settings.api_base, today_iso=today_iso, far_past_iso=far_past_iso)

    sentinel_secret = "SENTINEL-CLIENT-SECRET-DO-NOT-LEAK"
    sentinel_app_key = "SENTINEL-APP-KEY-DO-NOT-LEAK"
    leaky_settings = st_settings.model_copy(
        update={"client_secret": sentinel_secret, "app_key": sentinel_app_key}
    )

    st_exporter_logger = logging.getLogger("st_exporter")
    list_handler = _ListHandler()
    st_exporter_logger.addHandler(list_handler)
    try:
        with _frozen_now(), patch("logging.basicConfig"):
            logging.getLogger().setLevel(logging.DEBUG)
            run_export(
                leaky_settings,
                exporter_settings,
                export_store=InMemorySheetsStore(),
                raw_cache_store=InMemorySheetsStore(),
            )
    finally:
        st_exporter_logger.removeHandler(list_handler)

    # D1 gives st_exporter its own INFO-level output in production — confirm this
    # test actually captured something, not just that an empty list is secret-free.
    assert list_handler.records_captured, "expected INFO-level run.py logging to fire"

    captured = "\n".join(record.getMessage() for record in list_handler.records_captured)
    assert sentinel_secret not in captured
    assert sentinel_app_key not in captured
    assert sentinel_secret not in repr(leaky_settings)
    assert sentinel_app_key not in repr(leaky_settings)


@respx.mock
def test_jobs_only_run_does_not_touch_technicians_tab_or_raw_cache(
    st_settings, exporter_settings
) -> None:
    api_base = st_settings.api_base
    today_iso = FIXED_TODAY.isoformat()
    far_past_iso = (FIXED_TODAY - timedelta(days=200)).isoformat()

    export_store = InMemorySheetsStore()
    raw_cache_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)
    tenant_run1.register(api_base, today_iso=today_iso, far_past_iso=far_past_iso)

    with _frozen_now():
        summary = run_export(
            st_settings,
            exporter_settings,
            feeds=frozenset({"jobs"}),
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )

    assert summary.jobs_row_count == 1
    assert "jobs" in export_store.tabs
    assert "technicians" not in export_store.tabs
    # NOTE: the brief's original assertion here (`raw_cache_store.tabs == {}`)
    # is inconsistent with the jobs feed's own (unchanged) raw-cache
    # architecture: the raw cache exists precisely so the jobs feed can do
    # incremental/delta fetches across runs, and it is unconditionally
    # written on any non-dry-run that includes the jobs feed — this is the
    # same behavior the pre-existing `test_dry_run_reads_real_state_and_writes_nothing`
    # test already relies on (it captures a raw-cache snapshot right after a
    # normal, non-dry run and expects it non-trivially populated). A jobs-only
    # run necessarily populates the raw cache; removed as a plan/test bug
    # rather than an ambiguity resolvable in the implementation.
    assert set(raw_cache_store.tabs) == {
        "_raw_customers",
        "_raw_locations",
        "_raw_jobs",
        "_raw_appointments",
        "_raw_assignments",
    }, "jobs-only run should populate the raw cache (it owns incremental fetch state)"

    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert set(meta) == {"jobs"}


@respx.mock
def test_technicians_only_run_preserves_existing_jobs_tab_and_meta(
    st_settings, exporter_settings
) -> None:
    api_base = st_settings.api_base
    today_iso = FIXED_TODAY.isoformat()
    far_past_iso = (FIXED_TODAY - timedelta(days=200)).isoformat()

    export_store = InMemorySheetsStore()
    raw_cache_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)

    # Run 1: a normal both-feeds run establishes a jobs tab and jobs _meta row.
    tenant_run1.register(api_base, today_iso=today_iso, far_past_iso=far_past_iso)
    with _frozen_now():
        run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )
    jobs_tab_after_run1 = export_store.tabs["jobs"]
    jobs_meta_after_run1 = parse_meta_grid(export_store.tabs["_meta"])["jobs"]
    raw_cache_snapshot = {k: [row[:] for row in v] for k, v in raw_cache_store.tabs.items()}

    # Run 2: technicians-only. No new ServiceTitan routes are registered for the
    # jobs-side feeds (customers/locations/jobs/appointments/assignments) — if
    # _run() tried to fetch any of them, respx would raise for the unmocked call,
    # which is the proof this run touches nothing on the jobs side.
    respx.get(f"{api_base}/settings/v2/tenant/{st_settings.tenant_id}/technicians").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": 9, "name": "New Tech", "email": "nt@example.com", "active": True}],
                "hasMore": False,
                "continueFrom": None,
            },
        )
    )
    with _frozen_now():
        summary = run_export(
            st_settings,
            exporter_settings,
            feeds=frozenset({"technicians"}),
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )

    assert summary.technicians_row_count == 1
    assert summary.jobs_row_count == jobs_meta_after_run1.row_count
    assert export_store.tabs["jobs"] == jobs_tab_after_run1, "untouched feed's tab must survive"

    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert meta["jobs"] == jobs_meta_after_run1, (
        "untouched feed's _meta row must be carried forward"
    )
    assert meta["technicians"].row_count == 1

    assert raw_cache_store.tabs == raw_cache_snapshot, (
        "a technicians-only run must not touch the raw-cache sheet"
    )

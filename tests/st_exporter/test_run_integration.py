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

import respx

from st_exporter.meta import CursorBundle, parse_meta_grid
from st_exporter.run import run_export
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
def test_no_secret_leaks_even_with_root_logger_forced_to_debug(
    st_settings, exporter_settings, caplog
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

    caplog.set_level(logging.DEBUG)
    with _frozen_now(), patch("logging.basicConfig"):
        logging.getLogger().setLevel(logging.DEBUG)
        run_export(
            leaky_settings,
            exporter_settings,
            export_store=InMemorySheetsStore(),
            raw_cache_store=InMemorySheetsStore(),
        )

    captured = "\n".join(record.getMessage() for record in caplog.records)
    assert sentinel_secret not in captured
    assert sentinel_app_key not in captured
    assert sentinel_secret not in repr(leaky_settings)
    assert sentinel_app_key not in repr(leaky_settings)

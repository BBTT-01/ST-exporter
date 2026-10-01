"""End-to-end: `--feeds dispatch` through run_export, respx + in-memory Sheets.

The whole orchestration runs and only the HTTP transport is faked: the tab, its
`_meta` row, the carry-forward when another feed's run rewrites `_meta`, and the
two ways the tab can fail to refresh.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import respx

from st_exporter.dispatch import NON_JOB_APPOINTMENT_COLUMNS, NON_JOB_APPOINTMENTS_TAB
from st_exporter.meta import parse_meta_grid
from st_exporter.run import run_export
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token
from tests.st_exporter.fixtures import tenant_dispatch

FIXED_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
TAB = NON_JOB_APPOINTMENTS_TAB
NON_JOB_URL = "dispatch/v2/tenant/12345/non-job-appointments"
CODES_URL = "payroll/v2/tenant/12345/timesheet-codes"

EXPECTED_GRID = [
    list(NON_JOB_APPOINTMENT_COLUMNS),
    ["801", "9", "2026-09-15T17:00:00Z", "01:00:00", "false", "true", "false", "Lunch", "", ""],
    [
        "802",
        "10",
        "2026-09-16T14:30:00.123Z",
        "02:10:00.5000000",
        "false",
        "true",
        "true",
        "Training",
        "3",
        "TRAIN",
    ],
    ["803", "9", "2026-09-17T00:00:00Z", "23:59:59", "true", "true", "true", "PTO", "4", "PTO"],
]


def _run(st_settings, exporter_settings, export_store, *, feeds=frozenset({"dispatch"}), now=None):
    with patch("st_exporter.run.datetime", **{"now.return_value": now or FIXED_NOW}):
        return run_export(
            st_settings,
            exporter_settings,
            feeds=feeds,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )


def _route(api_base: str, path: str, response: httpx.Response) -> respx.Route:
    return respx.get(f"{api_base.rstrip('/')}/{path}").mock(return_value=response)


def _annotations(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::")]


@respx.mock
def test_writes_the_tab_and_a_dispatch_v1_meta_row(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_dispatch.register(st_settings.api_base)
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    assert export_store.tabs[TAB] == EXPECTED_GRID
    assert summary.dispatch_row_counts == {TAB: 3}
    assert summary.dispatch_failures == {}
    assert not summary.scope_not_granted and not summary.scope_revoked
    assert summary.feed_failures is None
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert set(meta) == {TAB}
    assert meta[TAB].contract_version == "dispatch.v1"
    assert meta[TAB].last_cursor == ""
    assert meta[TAB].last_run_at == FIXED_NOW.isoformat()
    assert meta[TAB].row_count == 3
    called = {c.request.url.path for c in respx.calls if not c.request.url.path.endswith("token")}
    assert called == {f"/{NON_JOB_URL}", f"/{CODES_URL}"}


@respx.mock
def test_the_window_reaches_servicetitan_as_query_parameters(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_dispatch.register(st_settings.api_base)

    _run(st_settings, exporter_settings, InMemorySheetsStore())

    [request] = [c.request for c in respx.calls if c.request.url.path == f"/{NON_JOB_URL}"]
    assert request.url.params["startsOnOrAfter"] == "2026-09-07T00:00:00Z"
    assert request.url.params["startsOnOrBefore"] == "2026-09-30T00:00:00Z"
    assert request.url.params["activeOnly"] == "true"


@respx.mock
def test_a_run_without_the_feed_carries_the_meta_row_forward(
    st_settings, exporter_settings
) -> None:
    """Every other feed job rewrites `_meta` whole, every few minutes. A tab its
    run does not carry would vanish from `_meta` on the next jobs run."""
    mock_auth_token(st_settings.auth_url)
    tenant_dispatch.register(st_settings.api_base)
    export_store = InMemorySheetsStore()
    _run(st_settings, exporter_settings, export_store)
    tab_before = [row[:] for row in export_store.tabs[TAB]]
    meta_before = parse_meta_grid(export_store.tabs["_meta"])

    for resource in ("technicians", "business-units"):
        _route(
            st_settings.api_base,
            f"settings/v2/tenant/12345/{resource}",
            httpx.Response(200, json={"data": [], "hasMore": False}),
        )
    first_run_calls = len(respx.calls)
    _run(
        st_settings,
        exporter_settings,
        export_store,
        feeds=frozenset({"technicians"}),
        now=LATER,
    )

    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert meta[TAB] == meta_before[TAB]
    assert meta["technicians"].last_run_at == LATER.isoformat()
    assert export_store.tabs[TAB] == tab_before
    later_paths = [c.request.url.path for c in list(respx.calls)[first_run_calls:]]
    assert not [path for path in later_paths if "non-job-appointments" in path]


@respx.mock
def test_a_tenant_never_granted_the_tab_stays_green_and_gets_no_tab(
    st_settings, exporter_settings, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    mock_auth_token(st_settings.auth_url)
    tenant_dispatch.register(st_settings.api_base)
    _route(st_settings.api_base, NON_JOB_URL, httpx.Response(403, text="Scope validation failed"))
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    assert TAB not in export_store.tabs
    assert TAB not in parse_meta_grid(export_store.tabs["_meta"])
    assert set(summary.scope_not_granted) == {TAB}
    assert not summary.scope_revoked and summary.feed_failures is None
    assert summary.dispatch_row_counts == {} and summary.dispatch_failures == {}
    assert not _annotations(capsys)
    assert not [c for c in respx.calls if "timesheet-codes" in c.request.url.path]


@respx.mock
def test_a_non_403_failure_stays_green_and_leaves_the_last_good_tab(
    st_settings, exporter_settings, caplog
) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_dispatch.register(st_settings.api_base)
    export_store = InMemorySheetsStore()
    _run(st_settings, exporter_settings, export_store)
    meta_before = parse_meta_grid(export_store.tabs["_meta"])

    _route(st_settings.api_base, NON_JOB_URL, httpx.Response(500, text="dispatch is unwell"))
    with caplog.at_level(logging.WARNING, logger="st_exporter"):
        summary = _run(st_settings, exporter_settings, export_store, now=LATER)

    assert TAB in (summary.dispatch_failures or {})
    assert TAB not in (summary.dispatch_row_counts or {})
    assert not summary.scope_not_granted and not summary.scope_revoked
    assert summary.feed_failures is None
    assert export_store.tabs[TAB] == EXPECTED_GRID
    assert parse_meta_grid(export_store.tabs["_meta"])[TAB] == meta_before[TAB]
    assert any(
        r.levelno == logging.WARNING and TAB in r.getMessage() and "NOT written" in r.getMessage()
        for r in caplog.records
    )


@respx.mock
def test_without_the_timesheet_codes_box_the_tab_is_written_with_blank_names(
    st_settings, exporter_settings, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    mock_auth_token(st_settings.auth_url)
    tenant_dispatch.register(st_settings.api_base)
    _route(st_settings.api_base, CODES_URL, httpx.Response(403, text="Scope validation failed"))
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    grid = export_store.tabs[TAB]
    names = [row[NON_JOB_APPOINTMENT_COLUMNS.index("timesheet_code_name")] for row in grid[1:]]
    ids = [row[NON_JOB_APPOINTMENT_COLUMNS.index("timesheet_code_id")] for row in grid[1:]]
    assert names == ["", "", ""]
    assert ids == ["", "3", "4"]
    assert summary.dispatch_row_counts == {TAB: 3}
    assert not summary.scope_not_granted and not summary.scope_revoked
    assert not _annotations(capsys)


@respx.mock
def test_a_dry_run_writes_nothing(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_dispatch.register(st_settings.api_base)
    export_store = InMemorySheetsStore()

    with patch("st_exporter.run.datetime", **{"now.return_value": FIXED_NOW}):
        summary = run_export(
            st_settings,
            exporter_settings,
            feeds=frozenset({"dispatch"}),
            dry_run=True,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )

    assert summary.dispatch_row_counts == {TAB: 3}
    assert export_store.tabs == {}

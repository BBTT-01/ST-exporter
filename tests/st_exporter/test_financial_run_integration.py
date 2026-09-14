"""End-to-end: `--feeds financial` through run_export, respx + in-memory Sheets.

This is the closest thing to the ticket's "against a real tenant" done-when that
is reachable without credentials: the whole orchestration runs, only the HTTP
transport is faked.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import respx

from st_exporter.financial import (
    BUSINESS_UNIT_COLUMNS,
    INVOICE_COLUMNS,
    JOB_COST_COLUMNS,
    TIMESHEET_COLUMNS,
)
from st_exporter.meta import parse_meta_grid
from st_exporter.run import (
    FINANCIAL_BUSINESS_UNITS_TAB,
    FINANCIAL_FEED_NAMES,
    FINANCIAL_INVOICES_TAB,
    FINANCIAL_JOB_COSTS_TAB,
    FINANCIAL_TIMESHEETS_TAB,
    run_export,
)
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token
from tests.st_exporter.fixtures import tenant_financial

FIXED_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _frozen_now():
    return patch("st_exporter.run.datetime", **{"now.return_value": FIXED_NOW})


def _run(st_settings, exporter_settings, export_store, feeds=frozenset({"financial"})):
    with _frozen_now():
        return run_export(
            st_settings,
            exporter_settings,
            feeds=feeds,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )


def _rows(grid):
    return [dict(zip(grid[0], row)) for row in grid[1:]]


@respx.mock
def test_writes_all_four_tabs_with_the_contract_headers(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    assert set(export_store.tabs) >= set(FINANCIAL_FEED_NAMES)
    assert export_store.tabs[FINANCIAL_INVOICES_TAB][0] == list(INVOICE_COLUMNS)
    assert export_store.tabs[FINANCIAL_TIMESHEETS_TAB][0] == list(TIMESHEET_COLUMNS)
    assert export_store.tabs[FINANCIAL_BUSINESS_UNITS_TAB][0] == list(BUSINESS_UNIT_COLUMNS)
    assert export_store.tabs[FINANCIAL_JOB_COSTS_TAB][0] == list(JOB_COST_COLUMNS)
    assert summary.financial_failures == {}
    assert summary.financial_row_counts == {
        FINANCIAL_INVOICES_TAB: 3,  # one row per LINE ITEM, from one invoice
        FINANCIAL_TIMESHEETS_TAB: 3,
        FINANCIAL_BUSINESS_UNITS_TAB: 2,
        FINANCIAL_JOB_COSTS_TAB: 2,  # the third report row has no JobNumber
    }


@respx.mock
def test_each_tab_gets_its_own_meta_row_with_the_contract_version(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    export_store = InMemorySheetsStore()

    _run(st_settings, exporter_settings, export_store)

    meta = parse_meta_grid(export_store.tabs["_meta"])
    for tab in FINANCIAL_FEED_NAMES:
        assert meta[tab].contract_version == "financial.v1"
        # Window-bounded full replace: nothing to carry forward, so no cursor.
        assert meta[tab].last_cursor == ""
        assert meta[tab].last_run_at == FIXED_NOW.isoformat()
        assert meta[tab].exporter_version
    assert meta[FINANCIAL_INVOICES_TAB].row_count == 3


@respx.mock
def test_the_window_is_sent_to_servicetitan_not_applied_locally(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    route = respx.get(f"{st_settings.api_base}/accounting/v2/tenant/12345/invoices")
    tenant_financial.register(st_settings.api_base)
    _run(st_settings, exporter_settings, InMemorySheetsStore())

    # 90 days back from 2026-09-14, at midnight UTC so two runs on the same day
    # ask for exactly the same range.
    assert route.calls.last.request.url.params["invoicedOnOrAfter"] == "2026-06-16T00:00:00Z"


@respx.mock
def test_the_report_is_run_with_profit_wizards_own_parameters(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    _run(st_settings, exporter_settings, InMemorySheetsStore())

    request = next(
        call.request
        for call in reversed(respx.calls)
        if call.request.method == "POST" and call.request.url.path.endswith("/reports/42/data")
    )
    body = json.loads(request.content)
    assert body == {
        "parameters": [
            {"name": "DateType", "value": 1},
            {"name": "From", "value": "2026-06-16"},
            {"name": "To", "value": "2026-09-14"},
        ]
    }
    # Paging rides in the query string; the POST is a read, not a mutation.
    assert request.url.params["page"] == "1"


@respx.mock
def test_a_missing_builtin_report_costs_only_its_own_tab(st_settings, exporter_settings) -> None:
    # The only namesake left is the contractor's custom report, which must be
    # refused rather than used. The other three tabs still land.
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base, report_present=False)
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    assert FINANCIAL_JOB_COSTS_TAB not in export_store.tabs
    assert FINANCIAL_JOB_COSTS_TAB in summary.financial_failures
    assert "custom" in summary.financial_failures[FINANCIAL_JOB_COSTS_TAB].lower()
    assert set(summary.financial_row_counts) == {
        FINANCIAL_INVOICES_TAB,
        FINANCIAL_TIMESHEETS_TAB,
        FINANCIAL_BUSINESS_UNITS_TAB,
    }


@respx.mock
def test_a_failed_tab_keeps_its_previous_contents_and_meta_row(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    export_store = InMemorySheetsStore()
    export_store.replace_grid(
        "_meta",
        [
            [
                "feed",
                "last_run_at",
                "last_cursor",
                "row_count",
                "exporter_version",
                "contract_version",
            ],
            [
                FINANCIAL_JOB_COSTS_TAB,
                "2026-09-13T00:00:00+00:00",
                "",
                "17",
                "0.2.8",
                "financial.v1",
            ],
        ],
    )
    export_store.replace_grid(
        FINANCIAL_JOB_COSTS_TAB, [list(JOB_COST_COLUMNS), ["J-OLD"] + [""] * 5]
    )

    tenant_financial.register(st_settings.api_base, report_present=False)
    _run(st_settings, exporter_settings, export_store)

    # Untouched, not emptied: last_run_at still says when it last genuinely ran.
    assert export_store.tabs[FINANCIAL_JOB_COSTS_TAB][1][0] == "J-OLD"
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert meta[FINANCIAL_JOB_COSTS_TAB].last_run_at == "2026-09-13T00:00:00+00:00"
    assert meta[FINANCIAL_JOB_COSTS_TAB].row_count == 17
    # ...while the tabs that succeeded got fresh rows.
    assert meta[FINANCIAL_INVOICES_TAB].last_run_at == FIXED_NOW.isoformat()


@respx.mock
def test_a_rate_limited_report_is_a_clean_skip_not_a_crash(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    respx.post(
        f"{st_settings.api_base}/reporting/v2/tenant/12345/report-category/operations"
        f"/reports/42/data"
    ).mock(return_value=httpx.Response(429, text="try again in 60 seconds"))
    export_store = InMemorySheetsStore()

    with patch("st_cli.client.time.sleep"):
        summary = _run(st_settings, exporter_settings, export_store)

    assert FINANCIAL_JOB_COSTS_TAB not in export_store.tabs
    assert "rate-limit" in summary.financial_failures[FINANCIAL_JOB_COSTS_TAB].lower()
    assert len(summary.financial_row_counts) == 3


@respx.mock
def test_not_selecting_the_feed_leaves_its_tabs_and_meta_rows_alone(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    export_store = InMemorySheetsStore()
    _run(st_settings, exporter_settings, export_store)
    before = {tab: [row[:] for row in grid] for tab, grid in export_store.tabs.items()}

    respx.get(f"{st_settings.api_base}/settings/v2/tenant/12345/technicians").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": False})
    )
    _run(st_settings, exporter_settings, export_store, feeds=frozenset({"technicians"}))

    for tab in FINANCIAL_FEED_NAMES:
        assert export_store.tabs[tab] == before[tab]
    meta = parse_meta_grid(export_store.tabs["_meta"])
    for tab in FINANCIAL_FEED_NAMES:
        assert meta[tab].last_run_at == FIXED_NOW.isoformat()
        assert meta[tab].contract_version == "financial.v1"


@respx.mock
def test_dry_run_computes_everything_and_writes_nothing(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    export_store = InMemorySheetsStore()

    with _frozen_now():
        summary = run_export(
            st_settings,
            exporter_settings,
            feeds=frozenset({"financial"}),
            dry_run=True,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )

    assert summary.financial_row_counts[FINANCIAL_JOB_COSTS_TAB] == 2
    assert export_store.tabs == {}

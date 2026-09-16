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


#: The fields `jpm/v2/.../jobs` will sort by, from its own published description:
#: "Available fields are: Id, ModifiedOn, CreatedOn, Priority." Anything else is
#: a 400 — `-completedOn` was, on a live tenant, and it cost the whole
#: `payroll.timesheets` tab because the job list is what drives the per-job
#: timesheet calls.
_SORTABLE_JOB_FIELDS = {"id", "modifiedon", "createdon", "priority"}


def _servicetitan_job_list(request: httpx.Request) -> httpx.Response:
    """The real endpoint's `sort` validation, transcribed from the live 400."""
    sort = request.url.params.get("sort")
    if sort is not None and sort.lstrip("+-").lower() not in _SORTABLE_JOB_FIELDS:
        return httpx.Response(
            400,
            json={
                "errors": {"sort": [f"The value '{sort}' is not valid for Sort."]},
                "title": "One or more validation errors occurred.",
                "status": 400,
            },
        )
    return httpx.Response(
        200,
        json={"data": tenant_financial.COMPLETED_JOBS, "hasMore": False, "totalCount": 2},
    )


@respx.mock
def test_the_job_list_sort_is_one_servicetitan_accepts(st_settings, exporter_settings) -> None:
    """Regression for run 35034278334 on `tr-pioneer-overhead-door` (0.2.11).

    The exporter sent `sort=-completedOn` and ServiceTitan answered
    `400 {"errors":{"sort":["The value '-completedOn' is not valid for Sort."]}}`,
    so `payroll.timesheets` was skipped on every run. Here the fixture endpoint
    validates `sort` exactly as the live one does: the old value fails this
    test, the new one writes the tab.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    route = respx.get(f"{st_settings.api_base}/jpm/v2/tenant/12345/jobs").mock(
        side_effect=_servicetitan_job_list
    )
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    params = dict(route.calls.last.request.url.params)
    print(f"\njpm/v2/tenant/12345/jobs request params: {params}")
    assert params == {
        "completedOnOrAfter": "2026-06-16T00:00:00Z",
        "sort": "-Id",
        "page": "1",
        "pageSize": "200",
    }
    # Not rejected, and the tab is written from a real answer rather than skipped.
    assert route.calls.last.response.status_code == 200
    assert FINANCIAL_TIMESHEETS_TAB not in summary.financial_failures
    assert summary.financial_row_counts[FINANCIAL_TIMESHEETS_TAB] == 3


@respx.mock
def test_the_400_sort_still_costs_only_its_own_tab(st_settings, exporter_settings) -> None:
    """The deliberate failure behaviour the live run showed, pinned.

    If the job list ever 400s again, the run must stay green, warn, skip only
    `payroll.timesheets`, and let the other three tabs through — never write an
    empty tab. This is the behaviour the fix preserves, not replaces.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    respx.get(f"{st_settings.api_base}/jpm/v2/tenant/12345/jobs").mock(
        return_value=httpx.Response(
            400, json={"errors": {"sort": ["The value '-completedOn' is not valid for Sort."]}}
        )
    )
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    assert FINANCIAL_TIMESHEETS_TAB in summary.financial_failures
    assert FINANCIAL_TIMESHEETS_TAB not in summary.financial_row_counts
    assert FINANCIAL_TIMESHEETS_TAB not in export_store.tabs
    assert export_store.tabs[FINANCIAL_BUSINESS_UNITS_TAB][0] == list(BUSINESS_UNIT_COLUMNS)
    assert export_store.tabs[FINANCIAL_JOB_COSTS_TAB][0] == list(JOB_COST_COLUMNS)


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
def test_a_marked_custom_namesake_is_refused_and_costs_only_its_own_tab(
    st_settings, exporter_settings
) -> None:
    # The only namesake left is the contractor's custom report, MARKED as
    # custom, which must be refused rather than used. The other three tabs land.
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
def test_an_unmarked_namesake_report_is_refused_on_its_columns(
    st_settings, exporter_settings
) -> None:
    """The case the NAME guard cannot see, and the one that costs real money.

    ServiceTitan's marker spelling is unverified, so a contractor's own "Job
    Costing Summary" may carry no marker at all. It then comes back as a single
    unambiguous match and every check upstream passes. Without a column check
    its rows zip against ITS field names, none of which are ours, so every money
    cell in the tab is blank — and the tab is written, `_meta` gets a healthy
    row_count, and `financial_failures` is empty. A blank cell is
    indistinguishable from a contractor with no costs.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base, report_present=False, custom_marked=False)
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    assert FINANCIAL_JOB_COSTS_TAB not in export_store.tabs
    failure = summary.financial_failures[FINANCIAL_JOB_COSTS_TAB]
    # Named, and specific about which columns were missing.
    assert "JobNumber" in failure and "TotalRevenue" in failure
    assert FINANCIAL_JOB_COSTS_TAB not in summary.financial_row_counts
    # The other three money tabs are untouched by one bad report.
    assert len(summary.financial_row_counts) == 3


@respx.mock
def test_a_renamed_field_on_the_real_report_fails_loudly_not_blankly(
    st_settings, exporter_settings
) -> None:
    # The other direction of the same guard: the report IS the built-in one, but
    # ServiceTitan has renamed a field we freeze. Without the check the tab goes
    # quietly empty forever; with it, the run says which column moved.
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    renamed = [
        {"name": "JobNumber"},
        {"name": "TotalRevenue"},
        {"name": "TotalCost"},  # was TotalCosts
        {"name": "MaterialEquipmentPurchaseOrderCosts"},
        {"name": "MaterialTotals"},
        {"name": "EquipmentCosts"},
    ]
    respx.get(
        f"{st_settings.api_base}/reporting/v2/tenant/12345/report-category/operations/reports/42"
    ).mock(
        return_value=httpx.Response(200, json={"name": "Job Costing Summary", "fields": renamed})
    )
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    assert FINANCIAL_JOB_COSTS_TAB not in export_store.tabs
    assert "TotalCosts" in summary.financial_failures[FINANCIAL_JOB_COSTS_TAB]


@respx.mock
def test_a_transport_failure_on_the_report_costs_only_its_own_tab(
    st_settings, exporter_settings
) -> None:
    """A timeout is not an HTTP status, and used to escape the per-tab guard.

    The report POST is the most timeout-prone call in the repo. Invoices,
    timesheets and business units have all been fetched successfully by the time
    it runs, so letting a bare ``httpx.ReadTimeout`` out discarded all three and
    left every `_meta` row stale — four tabs lost to one slow report.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    respx.post(
        f"{st_settings.api_base}/reporting/v2/tenant/12345/report-category/operations"
        f"/reports/42/data"
    ).mock(side_effect=httpx.ReadTimeout("the report took too long"))
    export_store = InMemorySheetsStore()

    with patch("st_cli.client.time.sleep"):
        summary = _run(st_settings, exporter_settings, export_store)

    # The three tabs that DID succeed are written, with fresh _meta rows.
    assert set(summary.financial_row_counts) == {
        FINANCIAL_INVOICES_TAB,
        FINANCIAL_TIMESHEETS_TAB,
        FINANCIAL_BUSINESS_UNITS_TAB,
    }
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert meta[FINANCIAL_INVOICES_TAB].last_run_at == FIXED_NOW.isoformat()
    # ...and the one that timed out is named, not silently absent.
    assert "ReadTimeout" in summary.financial_failures[FINANCIAL_JOB_COSTS_TAB]
    assert FINANCIAL_JOB_COSTS_TAB not in export_store.tabs


@respx.mock
def test_a_transport_failure_on_invoices_costs_only_the_invoices_tab(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_financial.register(st_settings.api_base)
    respx.get(f"{st_settings.api_base}/accounting/v2/tenant/12345/invoices").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    export_store = InMemorySheetsStore()

    with patch("st_cli.client.time.sleep"):
        summary = _run(st_settings, exporter_settings, export_store)

    assert FINANCIAL_INVOICES_TAB in summary.financial_failures
    assert export_store.tabs[FINANCIAL_JOB_COSTS_TAB][0] == list(JOB_COST_COLUMNS)
    assert len(summary.financial_row_counts) == 3


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

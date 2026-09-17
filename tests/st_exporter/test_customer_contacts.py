"""`customer_phone` / `customer_email`: the two cells that were blank on every row.

2,441 rows on one live tenant and 1,068 on the other, every one of them blank in
both columns, for the life of the feature. The exporter read the customer RECORD
— `customer.phone`, `phoneSettings[]`, `contacts[]` — and ServiceTitan keeps
those details on a different endpoint entirely
(``crm/v2/tenant/{id}/customers/{customerId}/contacts``), which is what
TradeRated's live Direct-path function has been reading all along.

This file pins the exporter half of the fix: the overlay onto the row, the fact
that the old readers still work underneath it, that a 403 costs two cells rather
than the feed, and — the one that would have caught the original bug — that the
blank-column detector stops firing for these two columns once contacts flow.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
import respx

from st_exporter.blank_columns import (
    ALL_BLANK_OK,
    MIN_ROWS_FOR_BLANK_COLUMN_WARNING,
    check_blank_columns,
)
from st_exporter.config import ExporterSettings
from st_exporter.denormalize import apply_customer_contacts, build_job_rows, customer_ids
from st_exporter.feeds.raw_cache import RawCache
from st_exporter.format import JOB_COLUMNS, build_job_grid
from st_exporter.meta import CursorBundle, parse_meta_grid
from st_exporter.run import run_export
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token
from tests.st_exporter.fixtures import tenant_run1

FIXED_TODAY = date(2026, 9, 3)
FIXED_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

PHONE_COL = JOB_COLUMNS.index("customer_phone")
EMAIL_COL = JOB_COLUMNS.index("customer_email")


def _frozen_now():
    return patch("st_exporter.run.datetime", **{"now.return_value": FIXED_NOW})


@pytest.fixture(autouse=True)
def _capture_exporter_logs(caplog):
    """``configure_logging`` sets ``propagate = False``; caplog needs it back on."""
    logger = logging.getLogger("st_exporter")
    previous = logger.propagate
    logger.propagate = True
    try:
        yield
    finally:
        logger.propagate = previous


# --- the overlay itself -------------------------------------------------------


def _rows(customer: dict, *, job_count: int = 1) -> list[dict]:
    """Denormalised rows for ``job_count`` jobs, all belonging to ``customer``."""
    raw_jobs = RawCache()
    raw_appointments = RawCache()
    for n in range(1, job_count + 1):
        raw_jobs.merge([{"id": n, "jobNumber": f"J-{n}", "customerId": customer["id"]}])
        raw_appointments.merge(
            [{"id": 100 + n, "jobId": n, "start": "2026-09-03T09:00:00Z", "end": None}]
        )
    raw_customers = RawCache()
    raw_customers.merge([customer])
    return build_job_rows(raw_jobs, raw_appointments, RawCache(), raw_customers, RawCache()).rows


CUSTOMER_WITH_NOTHING_ON_THE_RECORD = {"id": 10, "name": "Jane Doe"}


class TestTheContactsOverlay:
    def test_contacts_fill_both_columns_the_customer_record_left_blank(self) -> None:
        """The bug, in one assertion: this is the whole reported defect."""
        rows = _rows(CUSTOMER_WITH_NOTHING_ON_THE_RECORD)
        assert rows[0]["customer_phone"] is None and rows[0]["customer_email"] is None

        apply_customer_contacts(
            rows,
            {
                "10": [
                    {"type": "MobilePhone", "value": "555-0113"},
                    {"type": "Email", "value": "jane@example.invalid"},
                ]
            },
        )
        assert rows[0]["customer_phone"] == "555-0113"
        assert rows[0]["customer_email"] == "jane@example.invalid"

    def test_a_mobile_beats_a_landline_on_the_row(self) -> None:
        rows = _rows(CUSTOMER_WITH_NOTHING_ON_THE_RECORD)
        apply_customer_contacts(
            rows,
            {
                "10": [
                    {"type": "Phone", "value": "555-LAND"},
                    {"type": "MobilePhone", "value": "555-MOBILE"},
                ]
            },
        )
        assert rows[0]["customer_phone"] == "555-MOBILE"

    def test_a_fax_only_customer_gets_a_blank_phone_not_a_fax_number(self) -> None:
        rows = _rows(CUSTOMER_WITH_NOTHING_ON_THE_RECORD)
        apply_customer_contacts(rows, {"10": [{"type": "Fax", "value": "555-FAX"}]})
        assert rows[0]["customer_phone"] is None

    def test_a_customer_with_no_contacts_leaves_the_cells_blank_without_erroring(self) -> None:
        rows = _rows(CUSTOMER_WITH_NOTHING_ON_THE_RECORD)
        apply_customer_contacts(rows, {})
        assert rows[0]["customer_phone"] is None
        assert rows[0]["customer_email"] is None

    def test_an_email_only_customer_keeps_a_blank_phone_and_a_filled_email(self) -> None:
        rows = _rows(CUSTOMER_WITH_NOTHING_ON_THE_RECORD)
        apply_customer_contacts(rows, {"10": [{"type": "Email", "value": "jane@example.invalid"}]})
        assert rows[0]["customer_phone"] is None
        assert rows[0]["customer_email"] == "jane@example.invalid"

    def test_the_old_flat_readers_still_fill_the_cells_when_contacts_say_nothing(self) -> None:
        """Widen, never narrow: a tenant that DOES carry the flat scalars keeps working."""
        rows = _rows({"id": 10, "name": "Jane", "phone": "555-FLAT", "email": "flat@example.test"})
        apply_customer_contacts(rows, {})
        assert rows[0]["customer_phone"] == "555-FLAT"
        assert rows[0]["customer_email"] == "flat@example.test"

    def test_the_settings_array_reader_still_works_too(self) -> None:
        rows = _rows(
            {
                "id": 10,
                "name": "Jane",
                "phoneSettings": [{"phoneNumber": "555-ARRAY"}],
                "emailSettings": [{"email": "array@example.test"}],
            }
        )
        apply_customer_contacts(rows, {"10": []})
        assert rows[0]["customer_phone"] == "555-ARRAY"
        assert rows[0]["customer_email"] == "array@example.test"

    def test_the_contacts_endpoint_takes_precedence_over_the_record(self) -> None:
        rows = _rows({"id": 10, "name": "Jane", "phone": "555-FLAT", "email": "flat@example.test"})
        apply_customer_contacts(
            rows,
            {
                "10": [
                    {"type": "MobilePhone", "value": "555-REAL"},
                    {"type": "Email", "value": "real@example.test"},
                ]
            },
        )
        assert rows[0]["customer_phone"] == "555-REAL"
        assert rows[0]["customer_email"] == "real@example.test"

    def test_a_partial_answer_never_empties_a_cell_the_record_had_filled(self) -> None:
        """Precedence decides between two POPULATED values, and only then."""
        rows = _rows({"id": 10, "name": "Jane", "phone": "555-FLAT", "email": "flat@example.test"})
        apply_customer_contacts(rows, {"10": [{"type": "Email", "value": "real@example.test"}]})
        assert rows[0]["customer_phone"] == "555-FLAT"
        assert rows[0]["customer_email"] == "real@example.test"

    def test_the_join_key_never_reaches_a_cell(self) -> None:
        rows = _rows(CUSTOMER_WITH_NOTHING_ON_THE_RECORD)
        assert customer_ids(rows) == [10]
        apply_customer_contacts(rows, {})
        assert "_customer_id" not in rows[0]
        assert len(build_job_grid(rows)[0]) == len(JOB_COLUMNS)

    def test_one_customer_many_jobs_is_one_lookup_per_customer(self) -> None:
        rows = _rows(CUSTOMER_WITH_NOTHING_ON_THE_RECORD, job_count=3)
        assert customer_ids(rows) == [10, 10, 10]
        apply_customer_contacts(rows, {"10": [{"type": "Phone", "value": "555-1"}]})
        assert [row["customer_phone"] for row in rows] == ["555-1"] * 3


# --- the detector this bug got past ------------------------------------------


class TestTheBlankColumnDetector:
    """The cheap half of this bug class — and the reason not to exempt the columns."""

    @staticmethod
    def _grid(contacts: dict) -> list[list[str]]:
        rows = _rows(
            CUSTOMER_WITH_NOTHING_ON_THE_RECORD,
            job_count=MIN_ROWS_FOR_BLANK_COLUMN_WARNING + 1,
        )
        apply_customer_contacts(rows, contacts)
        return build_job_grid(rows)

    def test_it_stops_firing_once_contacts_return_data(self) -> None:
        blank = check_blank_columns(
            "jobs",
            self._grid(
                {
                    "10": [
                        {"type": "MobilePhone", "value": "555-0113"},
                        {"type": "Email", "value": "jane@example.invalid"},
                    ]
                }
            ),
        )
        assert "customer_phone" not in blank
        assert "customer_email" not in blank

    def test_and_still_fires_when_nothing_fills_them(self) -> None:
        """The control: without the fix this is what both live tenants produced."""
        blank = check_blank_columns("jobs", self._grid({}))
        assert "customer_phone" in blank
        assert "customer_email" in blank

    def test_neither_column_is_exempted_to_silence_the_detector(self) -> None:
        """Exempting them would hide the next occurrence of exactly this bug."""
        assert "customer_phone" not in ALL_BLANK_OK.get("jobs", {})
        assert "customer_email" not in ALL_BLANK_OK.get("jobs", {})


# --- the whole run ------------------------------------------------------------


def _register_run1(api_base: str) -> None:
    tenant_run1.register(
        api_base,
        today_iso=FIXED_TODAY.isoformat(),
        far_past_iso=(FIXED_TODAY - timedelta(days=200)).isoformat(),
    )


@respx.mock
def test_a_real_run_writes_the_contact_endpoints_values_into_the_tab(
    st_settings, exporter_settings
) -> None:
    export_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)
    _register_run1(st_settings.api_base)

    with _frozen_now():
        run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )

    row = export_store.tabs["jobs"][1]
    # The contact values, NOT the flat `phone`/`email` on CUSTOMER_10 — which is
    # what makes this a proof about which source was read.
    assert row[PHONE_COL] == "555-0113"
    assert row[EMAIL_COL] == "jane.mobile@example.com"
    assert tenant_run1.CUSTOMER_10["phone"] != row[PHONE_COL]


@respx.mock
def test_a_403_on_contacts_costs_two_cells_not_the_jobs_feed(
    st_settings, exporter_settings, monkeypatch, capsys, tmp_path
) -> None:
    """One column each, against every column of every row. Blank, loud, and green."""
    summary_file = tmp_path / "step_summary.md"
    summary_file.write_text("")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    export_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)
    _register_run1(st_settings.api_base)
    respx.get(
        f"{st_settings.api_base}/crm/v2/tenant/{tenant_run1.TENANT_ID}/customers/10/contacts"
    ).mock(return_value=httpx.Response(403, text="Forbidden"))

    with _frozen_now():
        summary = run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )

    # The tab still exported, in full, with its cursor advanced.
    assert summary.jobs_row_count == 1
    assert summary.feed_failures is None
    jobs = export_store.tabs["jobs"]
    assert len(jobs) == 2 and len(jobs[1]) == len(JOB_COLUMNS)
    assert jobs[1][JOB_COLUMNS.index("job_number")] == "J-1"
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert CursorBundle.decode(meta["jobs"].last_cursor).get("jobs") == tenant_run1.JOBS_CURSOR

    # The two cells fall back to the customer record (here, its flat scalars).
    assert jobs[1][PHONE_COL] == tenant_run1.CUSTOMER_10["phone"]

    # And the degradation is visible where a green run is actually read. Both
    # channels, because a WARNING in the log of a green run is invisible: nobody
    # opens a green run, which is exactly how this bug survived 2,441 rows.
    annotation = capsys.readouterr().out
    assert "::warning title=Customer contacts degraded::" in annotation
    assert "could not read customer contacts" in annotation
    assert "Customer contacts degraded" in summary_file.read_text()


@respx.mock
def test_a_409_on_one_inactive_customer_does_not_blank_every_rows_contacts(
    st_settings, exporter_settings
) -> None:
    """Live evidence from Door Serv Pro: ServiceTitan answers 409 "Customer ID =
    <id> is not active" for one inactive customer's contacts. Before this fix
    that single 409 escaped `fetch_contacts_per_customer` entirely, was caught
    by `_customer_contacts`'s degradation guard, and blanked
    customer_phone/customer_email on EVERY row of the run — not just the
    inactive customer's row(s). Job 2 (customer 11, active) must still resolve
    its own contacts."""
    export_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)
    _register_run1(st_settings.api_base)
    respx.get(
        f"{st_settings.api_base}/crm/v2/tenant/{tenant_run1.TENANT_ID}/customers/10/contacts"
    ).mock(return_value=httpx.Response(409, text="Customer ID = 10 is not active"))

    with _frozen_now():
        run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )

    row = export_store.tabs["jobs"][1]
    # Customer 10's row falls back to the flat customer-record scalar (the 409
    # cost only that customer's contacts overlay, not the whole run).
    assert row[PHONE_COL] == tenant_run1.CUSTOMER_10["phone"]


@respx.mock
def test_the_bulk_route_is_used_when_it_is_asked_for(st_settings, monkeypatch) -> None:
    monkeypatch.setenv("EXPORTER_CONTACTS_ROUTE", "export")
    settings = ExporterSettings(
        service_account_json="{}", sheet_id="export", raw_cache_sheet_id="raw"
    )
    export_store = InMemorySheetsStore()
    raw_cache_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)
    _register_run1(st_settings.api_base)
    bulk = respx.get(
        f"{st_settings.api_base}/crm/v2/tenant/{tenant_run1.TENANT_ID}/export/customers/contacts"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": 1, "customerId": 10, "type": "MobilePhone", "value": "555-BULK"}],
                "hasMore": False,
                "continueFrom": "contacts-c1",
            },
        )
    )

    with _frozen_now():
        run_export(
            st_settings,
            settings,
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )

    assert bulk.called
    assert export_store.tabs["jobs"][1][PHONE_COL] == "555-BULK"
    # Cached and cursor-tracked exactly like the other five feeds.
    assert raw_cache_store.tabs["_raw_customer_contacts"][1][0] == "1"
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert CursorBundle.decode(meta["jobs"].last_cursor).get("customer-contacts") == "contacts-c1"


@respx.mock
def test_a_tenant_without_the_bulk_feed_falls_back_instead_of_blanking_the_columns(
    st_settings, monkeypatch, capsys
) -> None:
    """The bulk route could not be proven to exist — so being wrong must be cheap."""
    monkeypatch.setenv("EXPORTER_CONTACTS_ROUTE", "export")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    settings = ExporterSettings(
        service_account_json="{}", sheet_id="export", raw_cache_sheet_id="raw"
    )
    export_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)
    _register_run1(st_settings.api_base)
    respx.get(
        f"{st_settings.api_base}/crm/v2/tenant/{tenant_run1.TENANT_ID}/export/customers/contacts"
    ).mock(return_value=httpx.Response(404, text="Not Found"))

    with _frozen_now():
        summary = run_export(
            st_settings,
            settings,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )

    assert summary.feed_failures is None
    assert export_store.tabs["jobs"][1][PHONE_COL] == "555-0113"  # per-customer route served it
    assert "::warning title=Bulk contacts feed absent::" in capsys.readouterr().out


def test_an_unknown_route_is_rejected_rather_than_quietly_fetching_nothing(monkeypatch) -> None:
    monkeypatch.setenv("EXPORTER_CONTACTS_ROUTE", "bulk")
    with pytest.raises(ValueError, match="EXPORTER_CONTACTS_ROUTE"):
        ExporterSettings(service_account_json="{}", sheet_id="e", raw_cache_sheet_id="r")

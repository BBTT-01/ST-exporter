"""The pure record -> row mapping for `dispatch.nonJobAppointments`.

Every assertion here runs without a client, a Sheet or a network — the same
property ``test_pricebook_bom.py`` and ``test_sales.py`` rely on to make the grid
recordable as a contract fixture.
"""

from __future__ import annotations

from typing import Any

from st_exporter import contracts, run
from st_exporter.dispatch import (
    CONTRACT_VERSION,
    NON_JOB_APPOINTMENT_COLUMNS,
    NON_JOB_APPOINTMENTS_TAB,
    TIMESHEET_CODE_NAME_FIELD,
    build_non_job_grid,
)
from st_exporter.scopes import TAB_PERMISSIONS
from tests.st_exporter.fixtures import tenant_dispatch


def _rows(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    grid = build_non_job_grid(records)
    assert grid[0] == list(NON_JOB_APPOINTMENT_COLUMNS)
    return [dict(zip(grid[0], row, strict=True)) for row in grid[1:]]


def _one(record: dict[str, Any]) -> dict[str, str]:
    rows = _rows([record])
    assert len(rows) == 1
    return rows[0]


class TestContract:
    def test_contract_version_string(self) -> None:
        assert CONTRACT_VERSION == "dispatch.v1"

    def test_the_columns_in_the_order_profit_wizard_reads_them(self) -> None:
        assert NON_JOB_APPOINTMENT_COLUMNS == (
            "st_non_job_appointment_id",
            "st_technician_id",
            "start",
            "duration",
            "all_day",
            "active",
            "remove_technician_from_capacity_planning",
            "name",
            "timesheet_code_id",
            "timesheet_code_name",
        )

    def test_the_tab_is_published_under_its_own_version(self) -> None:
        feed, tab = contracts.tabs()[NON_JOB_APPOINTMENTS_TAB]
        assert NON_JOB_APPOINTMENTS_TAB == "dispatch.nonJobAppointments"
        assert feed.feed == "dispatch"
        assert feed.version == "dispatch.v1"
        assert tab.columns == NON_JOB_APPOINTMENT_COLUMNS
        assert tab.row_key == ("st_non_job_appointment_id",)

    def test_the_contract_and_the_run_build_the_tab_the_same_way(self) -> None:
        _feed, tab = contracts.tabs()[NON_JOB_APPOINTMENTS_TAB]
        assert tab.build is build_non_job_grid
        assert run.DISPATCH_FEED_NAMES == (NON_JOB_APPOINTMENTS_TAB,)
        assert run.build_non_job_grid is build_non_job_grid
        assert run.DISPATCH_CONTRACT_VERSION == CONTRACT_VERSION

    def test_the_tab_has_a_permission_of_its_own(self) -> None:
        """Fetched, not derived: a 403 on it is its own, so the ledger keys on it."""
        assert NON_JOB_APPOINTMENTS_TAB in run.EXPORT_TABS
        assert TAB_PERMISSIONS[NON_JOB_APPOINTMENTS_TAB].startswith(
            "Dispatch -> Non-Job Appointments"
        )
        assert "Timesheet Codes" in TAB_PERMISSIONS[NON_JOB_APPOINTMENTS_TAB]


class TestRows:
    def test_a_full_record_maps_column_by_column(self) -> None:
        record = {**tenant_dispatch.TRAINING, TIMESHEET_CODE_NAME_FIELD: "TRAIN"}
        assert _one(record) == {
            "st_non_job_appointment_id": "802",
            "st_technician_id": "10",
            "start": "2026-09-16T14:30:00.123Z",
            "duration": "02:10:00.5000000",
            "all_day": "false",
            "active": "true",
            "remove_technician_from_capacity_planning": "true",
            "name": "Training",
            "timesheet_code_id": "3",
            "timesheet_code_name": "TRAIN",
        }

    def test_start_is_written_verbatim(self) -> None:
        for start in ("2026-09-15T17:00:00Z", "2026-09-16T14:30:00.123Z"):
            assert _one({"id": 1, "start": start})["start"] == start

    def test_duration_is_written_verbatim(self) -> None:
        """A .NET TimeSpan string, never converted: the end is the consumer's sum."""
        for duration in ("01:00:00", "02:10:00.5000000", "23:59:59", "1.02:00:00"):
            assert _one({"id": 1, "duration": duration})["duration"] == duration

    def test_booleans_are_lowercase(self) -> None:
        row = _one(
            {
                "id": 1,
                "allDay": True,
                "active": False,
                "removeTechnicianFromCapacityPlanning": True,
            }
        )
        assert (row["all_day"], row["active"], row["remove_technician_from_capacity_planning"]) == (
            "true",
            "false",
            "true",
        )

    def test_an_absent_boolean_is_blank_never_false(self) -> None:
        row = _one({"id": 1, "allDay": None})
        assert row["all_day"] == ""
        assert row["active"] == ""
        assert row["remove_technician_from_capacity_planning"] == ""

    def test_a_zero_timesheet_code_is_blank(self) -> None:
        """ServiceTitan's 0 is "no code", not a code whose id is zero."""
        assert _one({"id": 1, "timesheetCodeId": 0})["timesheet_code_id"] == ""
        assert _one({"id": 1, "timesheetCodeId": "0"})["timesheet_code_id"] == ""
        assert _one({"id": 1, "timesheetCodeId": None})["timesheet_code_id"] == ""
        assert _one({"id": 1})["timesheet_code_id"] == ""

    def test_a_real_timesheet_code_is_its_id_as_text(self) -> None:
        assert _one({"id": 1, "timesheetCodeId": 7})["timesheet_code_id"] == "7"

    def test_the_code_name_is_the_stamped_one_and_blank_when_unresolved(self) -> None:
        assert _one({"id": 1, TIMESHEET_CODE_NAME_FIELD: "PTO"})["timesheet_code_name"] == "PTO"
        assert _one({"id": 1, TIMESHEET_CODE_NAME_FIELD: None})["timesheet_code_name"] == ""
        assert _one({"id": 1, "timesheetCodeId": 7})["timesheet_code_name"] == ""

    def test_ids_are_text(self) -> None:
        row = _one({"id": 801, "technicianId": 9})
        assert (row["st_non_job_appointment_id"], row["st_technician_id"]) == ("801", "9")

    def test_a_record_without_an_id_is_dropped(self) -> None:
        records = [
            {"name": "no id at all"},
            {"id": None, "name": "null id"},
            {"id": "  ", "name": "blank id"},
            {"id": 801, "name": "Lunch"},
        ]
        assert [row["name"] for row in _rows(records)] == ["Lunch"]

    def test_a_repeated_id_is_written_once_first_kept(self) -> None:
        records = [{"id": 801, "name": "first"}, {"id": 802}, {"id": 801, "name": "again"}]
        rows = _rows(records)
        assert [row["st_non_job_appointment_id"] for row in rows] == ["801", "802"]
        assert rows[0]["name"] == "first"

    def test_records_keep_servicetitans_order(self) -> None:
        rows = _rows(tenant_dispatch.APPOINTMENTS)
        assert [row["st_non_job_appointment_id"] for row in rows] == ["801", "802", "803"]

    def test_no_records_is_a_header_only_grid(self) -> None:
        assert build_non_job_grid([]) == [list(NON_JOB_APPOINTMENT_COLUMNS)]

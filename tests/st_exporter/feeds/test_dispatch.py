"""Fetch-layer tests for `dispatch.nonJobAppointments`.

Routing, the server-side window, the out-of-window tripwire, and the optional
timesheet-code lookup. The parameter spellings matter more than they look:
ServiceTitan silently ignores one it does not recognise and answers with the
tenant's whole history, so a typo here is not an error anywhere.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from st_cli.exceptions import APIError, STCLIError
from st_exporter.dispatch import TIMESHEET_CODE_NAME_FIELD
from st_exporter.feeds.dispatch import (
    END_PARAM,
    START_PARAM,
    fetch_non_job_appointments,
    fetch_timesheet_code_names,
    warn_if_outside_window,
    warn_if_repeated_ids,
    window_bounds,
)
from tests.st_exporter.fixtures import tenant_dispatch

TODAY = date(2026, 9, 14)
START = datetime(2026, 9, 7, tzinfo=timezone.utc)
END = datetime(2026, 9, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _capture_exporter_logs(caplog):
    """``configure_logging`` sets ``propagate = False``; caplog needs it back on."""
    logger = logging.getLogger("st_exporter")
    previous = logger.propagate
    logger.propagate = True
    caplog.set_level(logging.DEBUG, logger="st_exporter")
    yield
    logger.propagate = previous


def _envelope(data: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": data, "hasMore": False}


def _client(
    appointments: list[dict[str, Any]] | None = None,
    codes: list[dict[str, Any]] | Exception | None = None,
) -> MagicMock:
    """A client answering the two list endpoints, routed by (module, resource)."""
    appointments = tenant_dispatch.APPOINTMENTS if appointments is None else appointments
    codes = tenant_dispatch.TIMESHEET_CODES if codes is None else codes

    def get(module: str, resource: str, params: dict[str, Any] | None = None) -> Any:
        if (module, resource) == ("dispatch", "non-job-appointments"):
            return _envelope(appointments)
        if (module, resource) == ("payroll", "timesheet-codes"):
            if isinstance(codes, Exception):
                raise codes
            return _envelope(codes)
        raise AssertionError(f"unexpected call: {module}/{resource}")

    client = MagicMock()
    client.get.side_effect = get
    return client


def _calls(client: MagicMock, module: str, resource: str) -> list[Any]:
    return [call for call in client.get.call_args_list if call.args[:2] == (module, resource)]


def _by_id(records: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    return {record["id"]: record for record in records}


class TestRouting:
    def test_appointments_come_from_dispatch_non_job_appointments(self) -> None:
        client = _client()
        fetch_non_job_appointments(client, today=TODAY)
        assert client.get.call_args_list[0].args[:2] == ("dispatch", "non-job-appointments")

    def test_code_names_come_from_payroll_timesheet_codes_retired_included(self) -> None:
        client = _client()
        fetch_non_job_appointments(client, today=TODAY)
        [call] = _calls(client, "payroll", "timesheet-codes")
        assert call.kwargs["params"]["active"] == "Any"
        assert call.kwargs["params"]["pageSize"] == 200

    def test_the_appointments_are_listed_before_any_payroll_call(self) -> None:
        """A tenant refused the appointments is refused before anything else."""
        client = _client(appointments=[])
        client.get.side_effect = APIError(403, "Scope validation failed")
        with pytest.raises(APIError):
            fetch_non_job_appointments(client, today=TODAY)
        assert client.get.call_count == 1
        assert client.get.call_args.args[:2] == ("dispatch", "non-job-appointments")


class TestWindow:
    def test_the_bounds_are_utc_midnights_seven_back_and_sixteen_ahead(self) -> None:
        assert window_bounds(TODAY) == (START, END)

    def test_the_window_is_sent_server_side_with_the_live_verified_spellings(self) -> None:
        client = _client()
        fetch_non_job_appointments(client, today=TODAY)
        [call] = _calls(client, "dispatch", "non-job-appointments")
        params = call.kwargs["params"]
        assert (START_PARAM, END_PARAM) == ("startsOnOrAfter", "startsOnOrBefore")
        assert params["startsOnOrAfter"] == "2026-09-07T00:00:00Z"
        assert params["startsOnOrBefore"] == "2026-09-30T00:00:00Z"
        assert params["activeOnly"] == "true"
        assert params["pageSize"] == 200

    def test_the_ignored_spellings_are_never_sent(self) -> None:
        """`startsBefore` and `active=True` were silently ignored on a live tenant."""
        client = _client()
        fetch_non_job_appointments(client, today=TODAY)
        [call] = _calls(client, "dispatch", "non-job-appointments")
        assert "startsBefore" not in call.kwargs["params"]
        assert "active" not in call.kwargs["params"]


class TestTheTripwire:
    def test_records_inside_the_window_are_quiet(self, caplog) -> None:
        warn_if_outside_window(tenant_dispatch.APPOINTMENTS, start=START, end=END)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_a_record_before_the_window_warns(self, caplog) -> None:
        warn_if_outside_window([{"start": "2020-01-01T00:00:00Z"}], start=START, end=END)
        assert "outside the requested window" in caplog.text
        assert "startsOnOrAfter" in caplog.text

    def test_a_record_after_the_window_warns(self, caplog) -> None:
        warn_if_outside_window([{"start": "2027-01-01T00:00:00.500Z"}], start=START, end=END)
        assert "outside the requested window" in caplog.text
        assert "startsOnOrBefore" in caplog.text

    def test_the_fetch_runs_the_tripwire_and_keeps_the_rows(self, caplog) -> None:
        """It only logs: filtering locally would hide the proof the filter broke."""
        stray = {**tenant_dispatch.LUNCH, "id": 900, "start": "2020-01-01T00:00:00Z"}
        records = fetch_non_job_appointments(_client(appointments=[stray]), today=TODAY)
        assert [record["id"] for record in records] == [900]
        assert "outside the requested window" in caplog.text

    def test_an_unparseable_start_is_not_a_crash(self, caplog) -> None:
        warn_if_outside_window([{"start": "not a date"}, {"start": None}], start=START, end=END)
        assert "outside the requested window" not in caplog.text


class TestRepeatedIds:
    def test_distinct_ids_are_quiet(self, caplog) -> None:
        warn_if_repeated_ids(tenant_dispatch.APPOINTMENTS)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_a_repeated_id_warns_with_the_count(self, caplog) -> None:
        records = [{"id": 1}, {"id": 2}, {"id": 1}, {"id": 1}, {"id": None}, {}]
        warn_if_repeated_ids(records)
        assert "2 non-job appointment record(s) repeated an id" in caplog.text

    def test_the_fetch_runs_it(self, caplog) -> None:
        twice = [tenant_dispatch.LUNCH, tenant_dispatch.LUNCH]
        fetch_non_job_appointments(_client(appointments=twice), today=TODAY)
        assert "repeated an id" in caplog.text


class TestTheCodeNames:
    def test_each_record_is_stamped_with_its_codes_name(self) -> None:
        records = _by_id(fetch_non_job_appointments(_client(), today=TODAY))
        assert records[802][TIMESHEET_CODE_NAME_FIELD] == "TRAIN"

    def test_a_retired_code_still_resolves(self) -> None:
        records = _by_id(fetch_non_job_appointments(_client(), today=TODAY))
        assert records[803][TIMESHEET_CODE_NAME_FIELD] == "PTO"

    def test_a_zero_code_resolves_to_nothing(self) -> None:
        records = _by_id(fetch_non_job_appointments(_client(), today=TODAY))
        assert records[801][TIMESHEET_CODE_NAME_FIELD] is None

    def test_an_unknown_code_resolves_to_nothing(self) -> None:
        unknown = {**tenant_dispatch.TRAINING, "timesheetCodeId": 99}
        [record] = fetch_non_job_appointments(_client(appointments=[unknown]), today=TODAY)
        assert record[TIMESHEET_CODE_NAME_FIELD] is None

    def test_the_record_is_otherwise_untouched(self) -> None:
        [record] = fetch_non_job_appointments(
            _client(appointments=[tenant_dispatch.TRAINING]), today=TODAY
        )
        assert {k: v for k, v in record.items() if k != TIMESHEET_CODE_NAME_FIELD} == (
            tenant_dispatch.TRAINING
        )

    def test_no_code_on_any_record_means_no_payroll_call(self) -> None:
        client = _client(appointments=[tenant_dispatch.LUNCH])
        [record] = fetch_non_job_appointments(client, today=TODAY)
        assert record[TIMESHEET_CODE_NAME_FIELD] is None
        assert not _calls(client, "payroll", "timesheet-codes")

    def test_a_code_with_no_code_text_is_not_a_name(self) -> None:
        client = _client(codes=[{"id": 3, "code": None}, {"id": 4, "code": "  "}])
        assert fetch_timesheet_code_names(client) == {}


class TestTheCodeNamesAreOptional:
    """Payroll -> Timesheet Codes is a box a Profit Wizard tenant may not have."""

    def test_a_403_blanks_the_names_and_keeps_every_record(self) -> None:
        client = _client(codes=APIError(403, "Scope validation failed"))
        records = fetch_non_job_appointments(client, today=TODAY)
        assert [record["id"] for record in records] == [801, 802, 803]
        assert all(record[TIMESHEET_CODE_NAME_FIELD] is None for record in records)

    def test_a_403_is_info_only_and_annotates_nothing(self, caplog, monkeypatch, capsys) -> None:
        """Quiet means quiet: a yellow mark on every run of every tenant without
        the optional box would train everybody to ignore the channel."""
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        fetch_non_job_appointments(
            _client(codes=APIError(403, "Scope validation failed")), today=TODAY
        )
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert "Payroll -> Timesheet Codes" in caplog.text
        assert not [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::")]

    @pytest.mark.parametrize(
        "error", [APIError(500, "upstream"), APIError(429, "slow down"), STCLIError("transport")]
    )
    def test_any_other_failure_degrades_the_same_way_but_is_announced(
        self, error, caplog, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        records = fetch_non_job_appointments(_client(codes=error), today=TODAY)
        assert all(record[TIMESHEET_CODE_NAME_FIELD] is None for record in records)
        assert "DEGRADED" in caplog.text
        annotations = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::")]
        assert annotations and annotations[0].startswith("::warning title=Timesheet codes degraded")

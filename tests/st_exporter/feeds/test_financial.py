"""Fetch-layer tests: module routing, the server-side window, and the N+1 timesheets.

The routing assertions exist because this repo has shipped two routing bugs of
exactly this kind (estimates in `salestech`, job types in `jpm`) — a wrong module
is a 404 against a real tenant and nothing at all against a mock.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from st_cli.exceptions import APIError, NotFoundError
from st_exporter.feeds.financial import (
    fetch_business_units,
    fetch_completed_job_ids,
    fetch_invoices,
    fetch_timesheets,
    job_cost_parameters,
    window_start,
)

TODAY = date(2026, 9, 14)


@pytest.fixture()
def mock_client():
    return MagicMock()


def _envelope(data, has_more=False):
    return {"data": data, "hasMore": has_more}


class TestRouting:
    def test_invoices_live_in_accounting(self, mock_client) -> None:
        mock_client.get.return_value = _envelope([])
        fetch_invoices(mock_client, today=TODAY)
        assert mock_client.get.call_args.args[:2] == ("accounting", "invoices")

    def test_business_units_live_in_settings(self, mock_client) -> None:
        mock_client.get.return_value = _envelope([])
        fetch_business_units(mock_client)
        assert mock_client.get.call_args.args[:2] == ("settings", "business-units")

    def test_completed_jobs_come_from_jpm(self, mock_client) -> None:
        mock_client.get.return_value = _envelope([])
        fetch_completed_job_ids(mock_client, today=TODAY)
        assert mock_client.get.call_args.args[:2] == ("jpm", "jobs")

    def test_timesheets_are_job_scoped_under_payroll(self, mock_client) -> None:
        # NOT the bulk `payroll/timesheets` list: that returns the payroll shape
        # (employeeId/startedOn), and the consumer reads the dispatch shape
        # (jobId/technicianId/arrivedOn/doneOn/canceledOn).
        mock_client.get.return_value = []
        fetch_timesheets(mock_client, ["7"])
        assert mock_client.get.call_args.args == ("payroll", "jobs/7/timesheets")


class TestWindow:
    def test_window_start_is_midnight_utc_on_the_cutoff_date(self) -> None:
        # Not "now minus N days": two runs on the same day must ask for the same
        # range, or a row appears to vanish between them.
        assert window_start(TODAY, window_days=90).isoformat() == "2026-06-16T00:00:00+00:00"

    def test_the_invoice_window_is_sent_as_a_server_side_filter(self, mock_client) -> None:
        mock_client.get.return_value = _envelope([])
        fetch_invoices(mock_client, today=TODAY, window_days=90)
        params = mock_client.get.call_args.kwargs["params"]
        assert params["invoicedOnOrAfter"] == "2026-06-16T00:00:00Z"

    def test_the_job_window_is_sent_as_a_server_side_filter(self, mock_client) -> None:
        mock_client.get.return_value = _envelope([])
        fetch_completed_job_ids(mock_client, today=TODAY, window_days=30)
        params = mock_client.get.call_args.kwargs["params"]
        assert params["completedOnOrAfter"] == "2026-08-15T00:00:00Z"

    def test_business_units_have_no_window_and_include_retired_ones(self, mock_client) -> None:
        mock_client.get.return_value = _envelope([])
        fetch_business_units(mock_client)
        params = mock_client.get.call_args.kwargs["params"]
        assert params["active"] == "Any"
        assert not any("On" in key for key in params)

    def test_report_parameters_match_profit_wizards_own_call(self) -> None:
        assert job_cost_parameters(TODAY, window_days=90) == [
            {"name": "DateType", "value": 1},
            {"name": "From", "value": "2026-06-16"},
            {"name": "To", "value": "2026-09-14"},
        ]


class TestTimesheets:
    def test_both_the_bare_array_and_the_envelope_forms_are_accepted(self, mock_client) -> None:
        mock_client.get.side_effect = [
            [{"id": 1, "jobId": 7}],
            {"data": [{"id": 2, "jobId": 8}]},
        ]
        segments = fetch_timesheets(mock_client, ["7", "8"])
        assert [s["id"] for s in segments] == [1, 2]

    def test_job_id_is_stamped_on_when_the_response_omits_it(self, mock_client) -> None:
        mock_client.get.return_value = [{"id": 1, "technicianId": 9}]
        assert fetch_timesheets(mock_client, ["7"])[0]["jobId"] == "7"

    def test_a_deleted_job_is_skipped_not_fatal(self, mock_client) -> None:
        # Losing the whole tab over one job deleted between the list call and
        # this one would be a self-inflicted outage that repeats every run.
        mock_client.get.side_effect = [NotFoundError("gone"), [{"id": 2, "jobId": 8}]]
        assert [s["id"] for s in fetch_timesheets(mock_client, ["7", "8"])] == [2]

    def test_a_non_404_api_error_still_propagates(self, mock_client) -> None:
        mock_client.get.side_effect = APIError(500, "boom")
        with pytest.raises(APIError):
            fetch_timesheets(mock_client, ["7"])

    def test_the_job_cap_bounds_the_request_count(self, mock_client) -> None:
        mock_client.get.return_value = _envelope([{"id": i} for i in range(10)], has_more=True)
        assert fetch_completed_job_ids(mock_client, today=TODAY, max_jobs=3) == ["0", "1", "2"]

    def test_a_job_without_an_id_is_dropped(self, mock_client) -> None:
        mock_client.get.return_value = _envelope([{"name": "no id"}, {"id": 5}])
        assert fetch_completed_job_ids(mock_client, today=TODAY) == ["5"]

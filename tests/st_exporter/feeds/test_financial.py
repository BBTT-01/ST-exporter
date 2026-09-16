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
    JOB_SORT,
    TimesheetPaginationError,
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


class TestJobListSort:
    """`jpm/v2/.../jobs` validates `sort` against a closed list and 400s otherwise.

    A live tenant (`tr-pioneer-overhead-door`, run 35034278334) answered
    `{"errors":{"sort":["The value '-completedOn' is not valid for Sort."]}}`,
    and the whole `payroll.timesheets` tab was skipped for it — the job list is
    what drives the per-job timesheet calls. The endpoint's published
    description names the only accepted fields: Id, ModifiedOn, CreatedOn,
    Priority.
    """

    def test_the_job_list_is_sorted_by_an_accepted_field(self, mock_client) -> None:
        mock_client.get.return_value = _envelope([])
        fetch_completed_job_ids(mock_client, today=TODAY, window_days=30)
        sort = mock_client.get.call_args.kwargs["params"]["sort"]
        assert sort == JOB_SORT
        assert sort.lstrip("+-") in {"Id", "ModifiedOn", "CreatedOn", "Priority"}

    def test_the_rejected_completedon_sort_is_never_sent_again(self, mock_client) -> None:
        # The exact parameter the live 400 named.
        mock_client.get.return_value = _envelope([])
        fetch_completed_job_ids(mock_client, today=TODAY, window_days=30)
        assert "completedOn" not in mock_client.get.call_args.kwargs["params"]["sort"]

    def test_the_whole_job_query_is_pinned(self, mock_client) -> None:
        # The full request a live run must now make, spelled out.
        mock_client.get.return_value = _envelope([])
        fetch_completed_job_ids(mock_client, today=TODAY, window_days=90)
        assert mock_client.get.call_args.kwargs["params"] == {
            "completedOnOrAfter": "2026-06-16T00:00:00Z",
            "sort": "-Id",
            "page": 1,
            "pageSize": 200,
        }

    def test_sorting_is_not_simply_dropped(self, mock_client) -> None:
        # Unsorted, ServiceTitan answers ascending by id — so the max_jobs cap
        # would keep the OLDEST jobs in the window rather than the newest.
        mock_client.get.return_value = _envelope([])
        fetch_completed_job_ids(mock_client, today=TODAY)
        assert mock_client.get.call_args.kwargs["params"].get("sort")


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

    def test_every_page_of_one_jobs_timesheets_is_read(self, mock_client) -> None:
        # A crew working a job over several days exceeds one page. Reading only
        # the first silently drops labour hours off the end, which reads
        # downstream as a cheaper job rather than as an error.
        mock_client.get.side_effect = [
            _envelope([{"id": 1, "jobId": 7}], has_more=True),
            _envelope([{"id": 2, "jobId": 7}], has_more=True),
            _envelope([{"id": 3, "jobId": 7}]),
        ]
        assert [s["id"] for s in fetch_timesheets(mock_client, ["7"])] == [1, 2, 3]
        pages = [c.kwargs["params"]["page"] for c in mock_client.get.call_args_list]
        assert pages == [1, 2, 3]

    def test_an_endless_hasmore_raises_rather_than_truncating(self, mock_client) -> None:
        """A server ignoring `page` answers hasMore forever with the same page.

        Writing the tab from those 20 pages reports a truncated (here: 20x
        duplicated) payroll as a complete success. It must be a NAMED failure,
        so `_TabGuard` skips the tab and keeps last run's contents.
        """
        mock_client.get.return_value = _envelope([{"id": 1, "jobId": 7}], has_more=True)
        with pytest.raises(TimesheetPaginationError) as excinfo:
            fetch_timesheets(mock_client, ["7"])
        assert "job 7" in str(excinfo.value)
        assert mock_client.get.call_count <= 21

    def test_a_bare_array_is_by_definition_the_whole_answer(self, mock_client) -> None:
        mock_client.get.return_value = [{"id": 1, "jobId": 7}]
        fetch_timesheets(mock_client, ["7"])
        assert mock_client.get.call_count == 1


class TestDateFilterTripwire:
    """ServiceTitan IGNORES query parameters it does not recognise.

    `invoicedOnOrAfter` / `completedOnOrAfter` are unverified spellings
    (KNOWN_UNVERIFIED.md), so a wrong one does not fail — it quietly returns the
    tenant's entire history and the feed exports all of it as if that were the
    window. The only signal available is the data itself.
    """

    def test_an_invoice_older_than_the_window_is_warned_about(self, mock_client, caplog) -> None:
        import logging

        mock_client.get.return_value = _envelope(
            [{"id": 1, "invoiceDate": "2020-01-01"}, {"id": 2, "invoiceDate": "2026-09-01"}]
        )
        with caplog.at_level(logging.WARNING, logger="st_exporter"):
            records = fetch_invoices(mock_client, today=TODAY)

        assert "invoicedOnOrAfter" in caplog.text
        assert "2020-01-01" in caplog.text
        # Logged only. Filtering locally would hide the very symptom that proves
        # the parameter name is wrong.
        assert len(records) == 2

    def test_records_inside_the_window_say_nothing(self, mock_client, caplog) -> None:
        import logging

        mock_client.get.return_value = _envelope([{"id": 1, "invoiceDate": "2026-09-01"}])
        with caplog.at_level(logging.WARNING, logger="st_exporter"):
            fetch_invoices(mock_client, today=TODAY)
        assert "predates" not in caplog.text

    def test_the_job_window_has_the_same_tripwire(self, mock_client, caplog) -> None:
        import logging

        mock_client.get.return_value = _envelope([{"id": 5, "completedOn": "2019-05-05T00:00:00Z"}])
        with caplog.at_level(logging.WARNING, logger="st_exporter"):
            assert fetch_completed_job_ids(mock_client, today=TODAY) == ["5"]
        assert "completedOnOrAfter" in caplog.text

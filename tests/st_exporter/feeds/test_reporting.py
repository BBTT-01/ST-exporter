"""The one guard that matters: the Job Costing Summary report is found by NAME.

Profit Wizard prefers a name match and then falls back to scoring every report by
its columns. These tests pin the exporter's refusal to do that — a contractor's
own report silently supplying cost numbers is the failure mode this whole module
exists to prevent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from st_cli.exceptions import RateLimitError
from st_exporter.feeds.reporting import (
    JOB_COSTING_SUMMARY_REPORT_NAME,
    JobCostingReportAmbiguousError,
    JobCostingReportNotFoundError,
    ReportColumnsMismatchError,
    ReportRateLimitedError,
    ReportRef,
    ReportUnavailableError,
    fetch_report_rows,
    field_names,
    find_job_costing_summary,
    require_columns,
)


def _envelope(data, has_more=False):
    return {"data": data, "hasMore": has_more}


def _client(categories, reports_by_category):
    """A client whose `get` answers the categories list then each reports list."""
    client = MagicMock()

    def get(module, resource, params=None):
        assert module == "reporting"
        if resource == "report-categories":
            return _envelope(categories)
        category_id = resource.split("/")[1]
        return _envelope(reports_by_category.get(category_id, []))

    client.get.side_effect = get
    return client


class TestDiscovery:
    def test_finds_the_builtin_report_by_exact_name(self) -> None:
        client = _client(
            [{"id": 7, "name": "Accounting"}],
            {"7": [{"id": 42, "name": "Job Costing Summary"}, {"id": 43, "name": "Sales"}]},
        )
        ref = find_job_costing_summary(client)
        assert ref == ReportRef(category_id="7", report_id="42", name="Job Costing Summary")

    def test_name_match_ignores_case_and_extra_whitespace(self) -> None:
        client = _client([{"id": 7}], {"7": [{"id": 42, "name": "  job   COSTING summary "}]})
        assert find_job_costing_summary(client).report_id == "42"

    def test_a_report_whose_name_merely_contains_it_is_not_a_match(self) -> None:
        # No substring matching: "Job Costing Summary (Dave's copy)" is a
        # contractor's spreadsheet, not ServiceTitan's report.
        client = _client(
            [{"id": 7}], {"7": [{"id": 99, "name": "Job Costing Summary (Dave's copy)"}]}
        )
        with pytest.raises(JobCostingReportNotFoundError):
            find_job_costing_summary(client)

    def test_absent_report_raises_a_named_error_and_never_a_best_match(self) -> None:
        client = _client(
            [{"id": 7}],
            {"7": [{"id": 1, "name": "Technician Scorecard"}, {"id": 2, "name": "Job Costs"}]},
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        assert JOB_COSTING_SUMMARY_REPORT_NAME in str(excinfo.value)

    @pytest.mark.parametrize(
        "marker",
        [
            {"isCustom": True},
            {"custom": True},
            {"isUserDefined": True},
            {"userDefined": True},
            {"type": "Custom"},
            {"reportType": "custom"},
            {"source": "UserDefined"},
        ],
    )
    def test_a_custom_report_with_the_right_name_is_refused_not_used(self, marker) -> None:
        client = _client([{"id": 7}], {"7": [{"id": 42, "name": "Job Costing Summary", **marker}]})
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        assert "custom" in str(excinfo.value).lower()

    def test_a_builtin_match_still_wins_when_a_custom_namesake_exists(self) -> None:
        client = _client(
            [{"id": 7}, {"id": 8}],
            {
                "7": [{"id": 42, "name": "Job Costing Summary"}],
                "8": [{"id": 99, "name": "Job Costing Summary", "isCustom": True}],
            },
        )
        assert find_job_costing_summary(client).report_id == "42"

    def test_two_distinct_builtin_namesakes_are_ambiguous_not_first_wins(self) -> None:
        client = _client(
            [{"id": 7}, {"id": 8}],
            {
                "7": [{"id": 42, "name": "Job Costing Summary"}],
                "8": [{"id": 43, "name": "Job Costing Summary"}],
            },
        )
        with pytest.raises(JobCostingReportAmbiguousError):
            find_job_costing_summary(client)

    def test_every_refusal_is_one_catchable_family(self) -> None:
        assert issubclass(JobCostingReportNotFoundError, ReportUnavailableError)
        assert issubclass(JobCostingReportAmbiguousError, ReportUnavailableError)
        assert issubclass(ReportRateLimitedError, ReportUnavailableError)
        assert issubclass(ReportColumnsMismatchError, ReportUnavailableError)


class TestReportData:
    ref = ReportRef(category_id="7", report_id="42", name="Job Costing Summary")

    def test_columnar_response_is_zipped_back_into_dicts(self) -> None:
        client = MagicMock()
        client.post.return_value = {
            "fields": [{"name": "JobId"}, {"name": "TotalCost"}],
            "data": [[1, "10.5"], [2, "20"]],
            "hasMore": False,
        }
        rows = fetch_report_rows(client, self.ref, parameters=[])
        assert rows == [{"JobId": 1, "TotalCost": "10.5"}, {"JobId": 2, "TotalCost": "20"}]

    def test_parameters_travel_in_the_body_and_paging_in_the_query(self) -> None:
        client = MagicMock()
        client.post.return_value = {"fields": [], "data": [], "hasMore": False}
        params = [{"name": "From", "value": "2026-06-16"}]
        fetch_report_rows(client, self.ref, parameters=params)

        module, resource = client.post.call_args.args
        assert module == "reporting"
        assert resource == "report-category/7/reports/42/data"
        assert client.post.call_args.kwargs["json_body"] == {"parameters": params}
        assert client.post.call_args.kwargs["params"] == {"page": 1, "pageSize": 200}

    def test_pagination_is_followed_and_fields_are_read_once(self) -> None:
        client = MagicMock()
        client.post.side_effect = [
            {"fields": [{"name": "JobId"}], "data": [[1]], "hasMore": True},
            {"data": [[2]], "hasMore": False},
        ]
        rows = fetch_report_rows(client, self.ref, parameters=[])
        assert rows == [{"JobId": 1}, {"JobId": 2}]
        assert [c.kwargs["params"]["page"] for c in client.post.call_args_list] == [1, 2]

    def test_a_rate_limit_is_a_named_stop_not_a_crash_and_not_a_partial_tab(self) -> None:
        client = MagicMock()
        client.post.side_effect = [
            {"fields": [{"name": "JobId"}], "data": [[1]], "hasMore": True},
            RateLimitError("slow down"),
        ]
        with pytest.raises(ReportRateLimitedError):
            fetch_report_rows(client, self.ref, parameters=[])

    def test_endless_hasmore_stops_rather_than_hammering_a_throttled_endpoint(self) -> None:
        client = MagicMock()
        client.post.return_value = {"fields": [{"name": "JobId"}], "data": [[1]], "hasMore": True}
        with pytest.raises(ReportRateLimitedError):
            fetch_report_rows(client, self.ref, parameters=[])
        assert client.post.call_count <= 51


class TestColumnGuard:
    """The name guard's blind spot: a namesake report that carries no marker.

    ``find_builtin_report`` returns it as a single unambiguous match, so nothing
    upstream refuses. Its columns are the only thing that gives it away, and
    without this check every money cell is blank, the tab is written, and
    `_meta` reports a healthy row_count — wrong money, reported as success.
    """

    ref = ReportRef(category_id="7", report_id="42", name="Job Costing Summary")
    required = ("JobNumber", "TotalCosts", "TotalRevenue")

    def test_a_report_missing_our_columns_is_refused_by_name(self) -> None:
        with pytest.raises(ReportColumnsMismatchError) as excinfo:
            require_columns(self.ref, ["Job", "Revenue", "Cost"], self.required, source="metadata")
        message = str(excinfo.value)
        # The message has to name what is missing, or the operator cannot act.
        assert "JobNumber" in message and "TotalCosts" in message and "TotalRevenue" in message
        assert "Job Costing Summary" in message

    def test_extra_columns_are_fine_because_the_tab_narrows_anyway(self) -> None:
        require_columns(
            self.ref,
            ["JobNumber", "TotalCosts", "TotalRevenue", "TechnicianName"],
            self.required,
            source="metadata",
        )

    def test_a_metadata_document_with_no_fields_defers_rather_than_refusing(self) -> None:
        # "Could not check here" is not "no columns" — the first data page's own
        # `fields` still gets checked, so the guard is not lost, only deferred.
        assert field_names({}) == []
        require_columns(self.ref, [], self.required, source="metadata")

    def test_field_names_reads_the_metadata_documents_own_order(self) -> None:
        assert field_names({"fields": [{"name": "A"}, {"name": "B"}]}) == ["A", "B"]
        assert field_names({"fields": "not a list"}) == []

    def test_the_data_response_is_checked_too_and_no_rows_are_kept(self) -> None:
        # Metadata and the data POST can disagree; it is the data response's own
        # columns that decide what lands in the tab.
        client = MagicMock()
        client.post.return_value = {
            "fields": [{"name": "Job"}, {"name": "Revenue"}],
            "data": [["J-7", 900]],
            "hasMore": True,
        }
        with pytest.raises(ReportColumnsMismatchError):
            fetch_report_rows(client, self.ref, parameters=[], required_columns=self.required)
        # Refused on the FIRST page — never paged on into a throttled endpoint.
        assert client.post.call_count == 1

    def test_the_right_report_passes_straight_through(self) -> None:
        client = MagicMock()
        client.post.return_value = {
            "fields": [{"name": n} for n in self.required],
            "data": [["J-7", 500, 900]],
            "hasMore": False,
        }
        rows = fetch_report_rows(client, self.ref, parameters=[], required_columns=self.required)
        assert rows == [{"JobNumber": "J-7", "TotalCosts": 500, "TotalRevenue": 900}]

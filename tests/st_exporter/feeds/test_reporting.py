"""The one guard that matters: the Job Costing Summary report is found by NAME.

Profit Wizard prefers a name match and then falls back to scoring every report by
its columns. These tests pin the exporter's refusal to do that — a contractor's
own report silently supplying cost numbers is the failure mode this whole module
exists to prevent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import respx

from st_cli.exceptions import RateLimitError
from st_exporter.feeds.reporting import (
    JOB_COSTING_SUMMARY_REPORT_NAME,
    JOB_COSTING_SUMMARY_REPORT_NAMES,
    JobCostingReportAmbiguousError,
    JobCostingReportNotFoundError,
    JobCostingReportPinNotFoundError,
    ReportColumnsMismatchError,
    ReportRateLimitedError,
    ReportRef,
    ReportUnavailableError,
    fetch_report_rows,
    field_names,
    find_job_costing_summary,
    require_columns,
)
from st_exporter.financial import JOB_COST_COLUMNS as _REQUIRED


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

    def test_the_ambiguous_refusal_names_the_reports_not_just_their_ids(self) -> None:
        client = _client(
            [{"id": 7}, {"id": 8}],
            {
                "7": [{"id": 42, "name": "Job Costing Summary"}],
                "8": [{"id": 43, "name": "job  costing  summary"}],
            },
        )
        with pytest.raises(JobCostingReportAmbiguousError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        assert "category 7/report 42" in message
        assert "category 8/report 43" in message
        assert "'job  costing  summary'" in message

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

    def test_a_data_page_with_no_fields_is_refused_not_deferred_again(self) -> None:
        """The data page is the BACKSTOP. It must never be a second deferral.

        Metadata with no `fields` defers to this page. If this page declares
        none either, every row zips to `{}`, `build_job_cost_grid` drops them
        all, and `reporting.jobCosts` is written with zero rows, a fresh `_meta`
        row and no recorded failure — the silent-blank-money outcome the guard
        exists for, reached THROUGH the guard.
        """
        client = MagicMock()
        client.post.return_value = {"data": [["J-7", 500, 900]], "hasMore": False}
        with pytest.raises(ReportColumnsMismatchError) as excinfo:
            fetch_report_rows(client, self.ref, parameters=[], required_columns=self.required)
        message = str(excinfo.value)
        assert "declared no fields" in message
        assert "TotalCosts" in message
        assert client.post.call_count == 1

    def test_a_differently_spelled_fields_key_is_refused_too(self) -> None:
        client = MagicMock()
        client.post.return_value = {
            "columns": [{"name": n} for n in self.required],
            "data": [["J-7", 500, 900]],
            "hasMore": False,
        }
        with pytest.raises(ReportColumnsMismatchError):
            fetch_report_rows(client, self.ref, parameters=[], required_columns=self.required)

    def test_a_caller_that_requires_nothing_is_still_free_to_read_anything(self) -> None:
        # The CLI's generic report reader passes no `required_columns`; it is
        # not building a fixed-column tab, so an unnamed shape is its business.
        client = MagicMock()
        client.post.return_value = {"data": [["J-7"]], "hasMore": False}
        assert fetch_report_rows(client, self.ref, parameters=[]) == [{}]

    def test_the_right_report_passes_straight_through(self) -> None:
        client = MagicMock()
        client.post.return_value = {
            "fields": [{"name": n} for n in self.required],
            "data": [["J-7", 500, 900]],
            "hasMore": False,
        }
        rows = fetch_report_rows(client, self.ref, parameters=[], required_columns=self.required)
        assert rows == [{"JobNumber": "J-7", "TotalCosts": 500, "TotalRevenue": 900}]


class TestTheDataPostIsTreatedAsAReadByTheRetryGate:
    """`POST .../data` must survive a ReadTimeout, because it mutates nothing.

    The round-two write-retry gate stopped retrying every non-GET. This endpoint
    is a read wearing a POST, so it was caught by a guard aimed at bookings and
    leads — and a report over a 90-day window against a 30s client timeout times
    out routinely, which would have failed `reporting.jobCosts` on every run.
    """

    @respx.mock
    def test_the_report_data_post_is_retried_on_a_read_timeout(self, st_settings) -> None:
        from unittest.mock import patch

        import httpx

        from st_cli.client import ServiceTitanClient
        from tests.st_exporter.conftest import mock_auth_token

        mock_auth_token(st_settings.auth_url)
        route = respx.post(
            f"{st_settings.api_base}/reporting/v2/tenant/12345/report-category/c/reports/r/data"
        ).mock(
            side_effect=[
                httpx.ReadTimeout("report generation > 30s"),
                httpx.Response(
                    200,
                    json={
                        "fields": [{"name": "JobNumber"}],
                        "data": [["J1"]],
                        "hasMore": False,
                    },
                ),
            ]
        )
        client = ServiceTitanClient(st_settings)
        try:
            with patch("st_cli.client.time.sleep"):
                rows = fetch_report_rows(
                    client,
                    ReportRef("c", "r", JOB_COSTING_SUMMARY_REPORT_NAME),
                    parameters=[],
                    required_columns=("JobNumber",),
                )
        finally:
            client.close()

        assert rows == [{"JobNumber": "J1"}]
        assert route.call_count == 2


class TestNotFoundDiagnostics:
    """A failed lookup has to say what it DID see.

    `reporting.jobCosts` failed on a live tenant with nothing but "no built-in
    report named 'Job Costing Summary' is visible to this tenant", which cannot
    distinguish "the scope is not really granted" from "it is named differently
    here" from "it genuinely is not there". These pin the evidence that settles
    that in one run.
    """

    def test_the_refusal_names_the_category_and_report_counts(self) -> None:
        client = _client(
            [{"id": 7, "name": "Accounting"}, {"id": 8, "name": "Operations"}],
            {
                "7": [{"id": 1, "name": "Invoice Aging"}, {"id": 2, "name": "Payroll Summary"}],
                "8": [{"id": 3, "name": "Technician Scorecard"}],
            },
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        assert "2 categories" in message
        assert "3 reports" in message
        assert "Invoice Aging" in message
        assert "Technician Scorecard" in message

    def test_successful_enumeration_does_not_blame_the_permission(self) -> None:
        client = _client([{"id": 7}], {"7": [{"id": 1, "name": "Invoice Aging"}]})
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        # The permission evidently works — saying "grant it" would send the
        # contractor down the wrong path a second time.
        assert "Grant the Reporting permission" not in message
        assert "permission is in place" in message

    def test_zero_categories_is_its_own_distinct_message(self) -> None:
        client = _client([], {})
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        assert "0 report categories and 0 reports" in message
        assert "Grant the Reporting permission" in message
        assert "permission is in place" not in message

    def test_categories_with_no_reports_is_also_distinct(self) -> None:
        client = _client([{"id": 7}, {"id": 8}], {})
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        assert "2 report categories" in message
        assert "contained 0 reports" in message
        assert "Grant the Reporting permission" in message

    @pytest.mark.parametrize(
        "near_miss",
        ["Job Cost Summary", "Job Costing Summary (Detail)", "Job Costs", "Costing Summary"],
    )
    def test_a_near_miss_name_is_highlighted_separately(self, near_miss) -> None:
        client = _client(
            [{"id": 7}],
            {"7": [{"id": 1, "name": "Invoice Aging"}, {"id": 2, "name": near_miss}]},
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        similar, _, other = message.partition("Other report names seen")
        assert "Similarly named reports seen" in similar
        assert near_miss in similar
        assert "Invoice Aging" in other

    def test_the_sample_is_capped_so_a_big_tenant_cannot_flood_the_log(self) -> None:
        reports = [{"id": n, "name": f"Report Number {n:04d}"} for n in range(500)]
        client = _client([{"id": 7}], {"7": reports})
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        assert "500 reports" in message
        assert f"of {len(reports)}" in message
        assert message.count("Report Number ") <= 18
        assert len(message) < 2000

    def test_one_absurdly_long_report_name_is_clipped(self) -> None:
        client = _client([{"id": 7}], {"7": [{"id": 1, "name": "X" * 5000}]})
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        assert len(str(excinfo.value)) < 1000

    def test_the_custom_report_path_keeps_its_own_distinct_message(self) -> None:
        client = _client(
            [{"id": 7}],
            {
                "7": [
                    {"id": 42, "name": "Job Costing Summary", "isCustom": True},
                    {"id": 43, "name": "Invoice Aging"},
                ]
            },
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        assert "are marked as custom reports, which are never used" in message
        # ...and still carries the census.
        assert "1 category" in message
        assert "2 reports" in message


class TestTheAcceptedNameSet:
    """ServiceTitan spells this report's name differently between tenants.

    Live run 35135903923 on `tr-doorservpro` (exporter 0.2.13) enumerated 12
    categories and 265 reports and found no "Job Costing Summary" at all — but
    the diagnostics' near-miss list carried "Job Costing Summary Report" twice,
    plus a "Project Costing Summary Report" that is a DIFFERENT report.

    So the feed accepts a short ordered list of exact names. These pin that the
    list widens *which exact strings count* and nothing else: no substring, no
    fuzziness, no second-choice name creeping past a first-choice one.
    """

    def test_the_documented_short_name_is_still_the_first_choice(self) -> None:
        assert JOB_COSTING_SUMMARY_REPORT_NAMES[0] == JOB_COSTING_SUMMARY_REPORT_NAME
        assert JOB_COSTING_SUMMARY_REPORT_NAMES == (
            "Job Costing Summary",
            "Job Costing Summary Report",
        )

    def test_the_short_name_still_matches_on_a_tenant_that_has_it(self) -> None:
        client = _client([{"id": 7}], {"7": [{"id": 42, "name": "Job Costing Summary"}]})
        assert find_job_costing_summary(client) == ReportRef("7", "42", "Job Costing Summary")

    def test_the_longer_name_matches_on_a_tenant_that_has_only_that(self) -> None:
        client = _client([{"id": 7}], {"7": [{"id": 42, "name": "Job Costing Summary Report"}]})
        assert find_job_costing_summary(client) == ReportRef(
            "7", "42", "Job Costing Summary Report"
        )

    def test_the_live_tenants_shape_selects_the_builtin_over_its_custom_namesake(self) -> None:
        """Run 35135903923's shape: two "Job Costing Summary Report" entries.

        The diagnostics' near-miss list is built from EVERY enumerated report —
        `_Census.saw_report` runs before both the name check and `_looks_custom`
        — so a duplicate in that list may be one built-in plus one contractor
        copy. That is this case: `_looks_custom` drops the copy and the built-in
        is selected unambiguously.
        """
        client = _client(
            [{"id": 11, "name": "Accounting"}, {"id": 12, "name": "My Reports"}],
            {
                "11": [
                    {"id": 501, "name": "Job Costing Summary Report"},
                    {"id": 502, "name": "Project Costing Summary Report"},
                ],
                "12": [
                    {"id": 900, "name": "Job Costing Summary Report", "isCustom": True},
                ],
            },
        )
        ref = find_job_costing_summary(client)
        # Visible under `pytest -s`: the outcome for the live tenant's shape.
        print(f"\n  run 35135903923 shape -> selected {ref}")
        assert ref == ReportRef("11", "501", "Job Costing Summary Report")

    def test_two_builtins_under_one_accepted_name_are_still_ambiguous(self) -> None:
        # The other reading of that duplicate in the near-miss list. It must
        # refuse, not pick: the two carry different numbers.
        client = _client(
            [{"id": 11}, {"id": 12}],
            {
                "11": [{"id": 501, "name": "Job Costing Summary Report"}],
                "12": [{"id": 502, "name": "Job Costing Summary Report"}],
            },
        )
        with pytest.raises(JobCostingReportAmbiguousError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        assert "'Job Costing Summary Report'" in message
        assert "category 11/report 501" in message and "category 12/report 502" in message

    def test_two_customs_under_one_accepted_name_take_the_custom_path(self) -> None:
        # The third reading: no built-in exists under that name either.
        client = _client(
            [{"id": 11}, {"id": 12}],
            {
                "11": [{"id": 501, "name": "Job Costing Summary Report", "isCustom": True}],
                "12": [{"id": 502, "name": "Job Costing Summary Report", "userDefined": True}],
            },
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        assert "are marked as custom reports, which are never used" in str(excinfo.value)

    def test_project_costing_summary_report_is_never_selected(self) -> None:
        """One token away from a name we accept, and a different report entirely.

        If it ever matched, the tab would fill with project-level figures under
        job-level headings — wrong money, reported as success.
        """
        client = _client(
            [{"id": 11}],
            {"11": [{"id": 777, "name": "Project Costing Summary Report"}]},
        )
        with pytest.raises(JobCostingReportNotFoundError):
            find_job_costing_summary(client)

    def test_project_costing_loses_even_sitting_beside_the_report_we_want(self) -> None:
        client = _client(
            [{"id": 11}],
            {
                "11": [
                    {"id": 777, "name": "Project Costing Summary Report"},
                    {"id": 501, "name": "Job Costing Summary Report"},
                ]
            },
        )
        assert find_job_costing_summary(client).report_id == "501"

    def test_the_first_choice_name_wins_when_both_names_exist_as_builtins(self) -> None:
        # Two DIFFERENT accepted names both matching is not ambiguity — it is
        # resolved by the declared order, deterministically and testably.
        client = _client(
            [{"id": 11}, {"id": 12}],
            {
                "12": [{"id": 502, "name": "Job Costing Summary Report"}],
                "11": [{"id": 501, "name": "Job Costing Summary"}],
            },
        )
        assert find_job_costing_summary(client) == ReportRef("11", "501", "Job Costing Summary")

    def test_a_second_choice_name_never_stands_in_for_a_first_choice_ambiguity(self) -> None:
        """The dangerous fall-through, pinned shut.

        If the first-choice name is ambiguous, moving on to the second-choice
        name would turn a loud refusal into a confident wrong answer.
        """
        client = _client(
            [{"id": 11}, {"id": 12}, {"id": 13}],
            {
                "11": [{"id": 501, "name": "Job Costing Summary"}],
                "12": [{"id": 502, "name": "Job Costing Summary"}],
                "13": [{"id": 503, "name": "Job Costing Summary Report"}],
            },
        )
        with pytest.raises(JobCostingReportAmbiguousError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        assert "'Job Costing Summary'" in message
        # It refused on the FIRST name; the second name's report is not offered
        # as the answer or blamed for the clash.
        assert "report 503" not in message

    def test_the_tenant_is_enumerated_only_once_for_the_whole_name_set(self) -> None:
        # Reporting is throttled hard; two accepted names must not mean two
        # full walks of every category.
        client = _client([{"id": 11}], {"11": [{"id": 501, "name": "Job Costing Summary"}]})
        find_job_costing_summary(client)
        assert client.get.call_count == 2  # categories, then category 11's reports


class TestTheDiagnosticsWithAPluralNameSet:
    """The refusal message has to stay accurate now that several names count."""

    def test_the_refusal_names_every_accepted_spelling(self) -> None:
        client = _client([{"id": 7}], {"7": [{"id": 1, "name": "Invoice Aging"}]})
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        assert "'Job Costing Summary' or 'Job Costing Summary Report'" in message
        assert "1 category" in message and "1 report" in message
        assert "permission is in place" in message

    def test_the_live_tenants_near_miss_list_still_renders(self) -> None:
        """The census sees every report, matched or not, custom or not.

        Reproduces run 35135903923's census against a name set that deliberately
        does NOT include what the tenant has, to prove the highlighting still
        fires for each accepted spelling.
        """
        client = _client(
            [{"id": 11}, {"id": 12}],
            {
                "11": [
                    {"id": 501, "name": "Job Costing Summary Report", "isCustom": True},
                    {"id": 502, "name": "Project Costing Summary Report", "isCustom": True},
                ],
                "12": [{"id": 900, "name": "Invoice Aging"}],
            },
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        similar, _, other = message.partition("Other report names seen")
        assert "Job Costing Summary Report" in similar
        assert "Project Costing Summary Report" in similar
        assert "Invoice Aging" in other
        # ...and the census counted the reports it never matched.
        assert "2 categories" in message and "3 reports" in message

    def test_a_near_miss_of_only_the_longer_name_is_highlighted_too(self) -> None:
        client = _client(
            [{"id": 7}],
            {"7": [{"id": 1, "name": "Invoice Aging"}, {"id": 2, "name": "Costing Report"}]},
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        similar, _, other = str(excinfo.value).partition("Other report names seen")
        assert "Costing Report" in similar
        assert "Invoice Aging" in other

    def test_the_custom_only_refusal_also_names_every_accepted_spelling(self) -> None:
        client = _client(
            [{"id": 7}],
            {"7": [{"id": 42, "name": "Job Costing Summary Report", "isCustom": True}]},
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)
        message = str(excinfo.value)
        assert "'Job Costing Summary' or 'Job Costing Summary Report'" in message
        assert "are marked as custom reports, which are never used" in message


class TestTwoReportsSharingOneName:
    """`tr-doorservpro` has two built-in reports both named 'Job Costing Summary
    Report' (run 35155266960: ids 21131704 and 21639096, both in `operations`,
    neither marked custom), so the tab has been refused on every run.

    Two ways out, and neither is a heuristic. Columns can ELIMINATE a candidate
    that could not have produced the tab at all; a human can PIN the id. What is
    still refused, and must stay refused, is choosing between two reports that
    are both capable — two copies of one report look exactly like that.
    """

    def _two_named(self, fields_by_id):
        client = _client(
            [{"id": "operations", "name": "Operations"}],
            {
                "operations": [
                    {"id": 21131704, "name": "Job Costing Summary Report"},
                    {"id": 21639096, "name": "Job Costing Summary Report"},
                ]
            },
        )
        real_get = client.get.side_effect

        def get(module, resource, params=None):
            if resource.startswith("report-category/") and resource.count("/") == 3:
                return {"fields": [{"name": n} for n in fields_by_id[resource.rsplit("/", 1)[-1]]]}
            return real_get(module, resource, params)

        client.get.side_effect = get
        return client

    def test_the_only_capable_report_is_selected(self) -> None:
        """The impostor cannot produce the tab, so dropping it takes nothing away."""
        client = self._two_named(
            {
                "21131704": ["JobNumber", "SomethingElse"],
                "21639096": list(_REQUIRED),
            }
        )
        ref = find_job_costing_summary(client, required_columns=_REQUIRED)
        assert ref.report_id == "21639096"

    def test_two_capable_reports_are_still_ambiguous(self) -> None:
        """The case that must NOT be resolved. Two copies of one report both
        declare the right columns, and preferring either is the silent-wrong-money
        failure this module exists to prevent."""
        client = self._two_named({"21131704": list(_REQUIRED), "21639096": list(_REQUIRED)})
        with pytest.raises(JobCostingReportAmbiguousError) as excinfo:
            find_job_costing_summary(client, required_columns=_REQUIRED)
        assert "2 of them declare the columns" in str(excinfo.value)

    def test_no_capable_report_is_still_ambiguous(self) -> None:
        client = self._two_named({"21131704": ["Nope"], "21639096": ["AlsoNope"]})
        with pytest.raises(JobCostingReportAmbiguousError) as excinfo:
            find_job_costing_summary(client, required_columns=_REQUIRED)
        assert "None of them declares" in str(excinfo.value)

    def test_a_candidate_declaring_no_fields_makes_it_unresolvable(self) -> None:
        """Absent metadata is "could not check", not "no columns". Counting it as
        incapable would eliminate a report on missing evidence — and if that were
        the real one, it would silently select the impostor."""
        client = self._two_named({"21131704": [], "21639096": list(_REQUIRED)})
        with pytest.raises(JobCostingReportAmbiguousError) as excinfo:
            find_job_costing_summary(client, required_columns=_REQUIRED)
        assert "could not be compared" in str(excinfo.value)

    def test_without_required_columns_it_refuses_exactly_as_before(self) -> None:
        """Elimination is opt-in. A caller that names no columns gets the old
        behaviour, and no metadata request is made on its behalf."""
        client = self._two_named({"21131704": list(_REQUIRED), "21639096": ["x"]})
        with pytest.raises(JobCostingReportAmbiguousError):
            find_job_costing_summary(client)

    def test_the_refusal_names_both_ids_and_how_to_resolve_it(self) -> None:
        client = self._two_named({"21131704": list(_REQUIRED), "21639096": list(_REQUIRED)})
        with pytest.raises(JobCostingReportAmbiguousError) as excinfo:
            find_job_costing_summary(client, required_columns=_REQUIRED)
        message = str(excinfo.value)
        assert "21131704" in message and "21639096" in message
        assert "EXPORTER_JOB_COST_REPORT_ID" in message


class TestThePinnedReportId:
    def _tenant(self):
        return _client(
            [{"id": "operations", "name": "Operations"}],
            {
                "operations": [
                    {"id": 21131704, "name": "Job Costing Summary Report"},
                    {"id": 21639096, "name": "Job Costing Summary Report"},
                ]
            },
        )

    def test_a_pin_resolves_what_the_name_cannot(self) -> None:
        ref = find_job_costing_summary(self._tenant(), pinned_report_id="21639096")
        assert ref == ReportRef("operations", "21639096", "Job Costing Summary Report")

    def test_a_pin_wins_over_name_resolution_entirely(self) -> None:
        """Pinning a differently-named report selects it. The pin is the decision;
        the names stop mattering once one exists."""
        client = _client(
            [{"id": "7", "name": "Accounting"}],
            {
                "7": [
                    {"id": 42, "name": JOB_COSTING_SUMMARY_REPORT_NAME},
                    {"id": 99, "name": "Some Other Report"},
                ]
            },
        )
        assert find_job_costing_summary(client, pinned_report_id="99").report_id == "99"

    def test_a_pin_selects_a_report_marked_custom(self) -> None:
        """A human who pins a custom report has decided that on purpose — the
        guard exists to stop the CODE choosing one, not to overrule the operator.
        The column checks in `fetch_job_costs` still apply to it."""
        client = _client(
            [{"id": "7", "name": "Accounting"}],
            {"7": [{"id": 42, "name": "Contractor's Own", "isCustom": True}]},
        )
        assert find_job_costing_summary(client, pinned_report_id="42").report_id == "42"

    def test_a_pin_the_tenant_does_not_have_is_refused_not_fallen_back_from(self) -> None:
        """The important one. Falling back to the name would quietly undo the
        decision and could re-select the very report the pin was added to avoid."""
        with pytest.raises(JobCostingReportPinNotFoundError) as excinfo:
            find_job_costing_summary(self._tenant(), pinned_report_id="404404")
        message = str(excinfo.value)
        assert "404404" in message
        assert "EXPORTER_JOB_COST_REPORT_ID" in message

    def test_surrounding_whitespace_in_the_pin_is_tolerated(self) -> None:
        """It arrives through a workflow input and a shell env var."""
        ref = find_job_costing_summary(self._tenant(), pinned_report_id="  21639096 ")
        assert ref.report_id == "21639096"

    def test_no_pin_is_the_unchanged_path(self) -> None:
        with pytest.raises(JobCostingReportAmbiguousError):
            find_job_costing_summary(self._tenant(), pinned_report_id=None)


class TestTheCustomRefusalCarriesItsEvidence:
    """A name-matching report skipped as custom must say WHICH marker fired.

    `_CUSTOM_BOOLEAN_FIELDS` / `_CUSTOM_KIND_FIELDS` are guesses at a spelling
    never seen on a real tenant (KNOWN_UNVERIFIED.md), and the asymmetry that
    makes the guesses safe — a false positive costs a loud refusal, a false
    negative costs silently wrong money — only holds if the loud refusal is
    actually actionable. It was not: it said the matching reports were custom
    and stopped, which points the contractor at a report they can already see
    and tells whoever is debugging nothing about which guess fired.

    These pin the evidence without touching selection: `custom_marker` is the
    same rule `_looks_custom` was, and `TestDiscovery` still owns what gets
    chosen.
    """

    def test_it_names_the_report_and_the_marker_that_skipped_it(self) -> None:
        client = _client(
            [{"id": 7, "name": "Accounting"}],
            {"7": [{"id": 42, "name": JOB_COSTING_SUMMARY_REPORT_NAME, "isCustom": True}]},
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)

        message = str(excinfo.value)
        assert "category 7/report 42" in message
        assert "isCustom=true" in message

    def test_it_quotes_a_kind_field_value_rather_than_just_the_field(self) -> None:
        client = _client(
            [{"id": 7, "name": "Accounting"}],
            {"7": [{"id": 42, "name": JOB_COSTING_SUMMARY_REPORT_NAME, "reportType": "Custom"}]},
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)

        assert "reportType='Custom'" in str(excinfo.value)

    def test_it_says_the_marker_spellings_are_unverified(self) -> None:
        """The point of the whole message: this may be a false positive on a
        guessed field name, not a missing report."""
        client = _client(
            [{"id": 7, "name": "Accounting"}],
            {"7": [{"id": 42, "name": JOB_COSTING_SUMMARY_REPORT_NAME, "isCustom": True}]},
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)

        message = str(excinfo.value)
        assert "NOT confirmed" in message
        assert "false" in message and "positive" in message

    def test_it_names_every_skipped_report_when_several_match(self) -> None:
        client = _client(
            [{"id": 7, "name": "Accounting"}, {"id": 8, "name": "Ops"}],
            {
                "7": [{"id": 42, "name": JOB_COSTING_SUMMARY_REPORT_NAME, "isCustom": True}],
                "8": [{"id": 99, "name": JOB_COSTING_SUMMARY_REPORT_NAME, "userDefined": True}],
            },
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)

        message = str(excinfo.value)
        assert "category 7/report 42" in message
        assert "category 8/report 99" in message

    def test_a_builtin_beside_a_custom_namesake_is_still_selected(self) -> None:
        """Selection is untouched — the evidence is only collected on the path
        that ends in a refusal."""
        client = _client(
            [{"id": 7, "name": "Accounting"}],
            {
                "7": [
                    {"id": 42, "name": JOB_COSTING_SUMMARY_REPORT_NAME, "isCustom": True},
                    {"id": 43, "name": JOB_COSTING_SUMMARY_REPORT_NAME},
                ]
            },
        )
        assert find_job_costing_summary(client).report_id == "43"

    def test_a_report_with_no_marker_produces_no_custom_evidence(self) -> None:
        """A tenant where nothing is named right must keep the plain not-found
        message, with no mention of custom reports to chase."""
        client = _client(
            [{"id": 7, "name": "Accounting"}],
            {"7": [{"id": 42, "name": "Sales By Technician"}]},
        )
        with pytest.raises(JobCostingReportNotFoundError) as excinfo:
            find_job_costing_summary(client)

        assert "custom" not in str(excinfo.value).lower()


class TestTheRateLimitBudgetForOneReport:
    """The client now waits as long as ServiceTitan asks, which is what makes a
    paginated report reachable — and is also why one pull needs a ceiling.

    Reporting counts each PAGE as a run of the report, so a multi-page pull is
    throttled by its own previous page and legitimately takes minutes. "Wait as
    long as you are told, every page, forever" is how a run gets SIGKILLed by the
    runner with nothing written and nothing learned.
    """

    def _client_that_is_always_throttled(self, seconds: float):
        client = MagicMock()
        client.on_rate_limited = None

        def post(module, resource, json_body=None, params=None, idempotent=False):
            # Stand in for the client's own internal sleep: it calls the governor
            # with the seconds it is about to wait, then eventually succeeds.
            if client.on_rate_limited is not None:
                client.on_rate_limited(seconds)
            return {"fields": [{"name": "JobNumber"}], "data": [["1"]], "hasMore": True}

        client.post.side_effect = post
        return client

    def test_a_pull_that_parks_too_long_is_skipped_not_left_running(self) -> None:
        client = self._client_that_is_always_throttled(60.0)
        with pytest.raises(ReportRateLimitedError) as excinfo:
            fetch_report_rows(client, ReportRef("7", "42", "R"), parameters=[])
        message = str(excinfo.value)
        assert "rate limiter" in message
        assert "unaffected" in message

    def test_the_governor_the_caller_already_had_is_restored(self) -> None:
        """`fetch_report_rows` borrows `on_rate_limited` to measure the wait. The
        image pass sets that hook for real, so leaving it replaced would silently
        disconnect a shared rate limiter for the rest of the run."""
        client = self._client_that_is_always_throttled(60.0)
        sentinel = MagicMock()
        client.on_rate_limited = sentinel
        with pytest.raises(ReportRateLimitedError):
            fetch_report_rows(client, ReportRef("7", "42", "R"), parameters=[])
        assert client.on_rate_limited is sentinel

    def test_an_existing_governor_still_hears_every_wait(self) -> None:
        client = self._client_that_is_always_throttled(60.0)
        heard: list[float] = []
        client.on_rate_limited = heard.append
        with pytest.raises(ReportRateLimitedError):
            fetch_report_rows(client, ReportRef("7", "42", "R"), parameters=[])
        assert heard and set(heard) == {60.0}

    def test_a_report_that_is_never_throttled_is_unaffected(self) -> None:
        client = MagicMock()
        client.on_rate_limited = None
        client.post.return_value = {
            "fields": [{"name": "JobNumber"}],
            "data": [["1"], ["2"]],
            "hasMore": False,
        }
        rows = fetch_report_rows(client, ReportRef("7", "42", "R"), parameters=[])
        assert rows == [{"JobNumber": "1"}, {"JobNumber": "2"}]

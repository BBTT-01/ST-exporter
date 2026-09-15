"""The whole-column-blank detector (ticket 25's detection half).

The acceptance test for the ticket is
``test_jobs_grid_with_blank_job_number_is_caught`` — it builds a jobs grid the way
``_run_jobs_feed`` does from rows that carry no ``job_number``, which is exactly the
shape the original bug produced (the exporter read ``number``, ServiceTitan spells
it ``jobNumber``, and 2431 rows of a real Sheet went out blank with the suite green).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
import respx

from st_exporter.blank_columns import (
    ALL_BLANK_OK,
    MIN_ROWS_FOR_BLANK_COLUMN_WARNING,
    check_blank_columns,
)
from st_exporter.format import JOB_COLUMNS, TECHNICIAN_COLUMNS, format_job_row
from st_exporter.meta import MetaRow
from st_exporter.run import _run_technicians_feed, _TabGuard, run_export
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token
from tests.st_exporter.fixtures import tenant_run1

ABOVE = MIN_ROWS_FOR_BLANK_COLUMN_WARNING
BELOW = MIN_ROWS_FOR_BLANK_COLUMN_WARNING - 1


@pytest.fixture(autouse=True)
def _capture_exporter_logs(caplog):
    """``configure_logging`` sets ``propagate = False``; caplog needs it back on."""
    logger = logging.getLogger("st_exporter")
    previous = logger.propagate
    logger.propagate = True
    caplog.set_level(logging.DEBUG, logger="st_exporter")
    yield
    logger.propagate = previous


def _grid(rows: int, *, second_column: str = "") -> list[list[str]]:
    return [["st_id", "suspect"]] + [[str(i), second_column] for i in range(rows)]


class TestThreshold:
    def test_all_blank_column_above_threshold_warns(self, caplog) -> None:
        assert check_blank_columns("jobs", _grid(ABOVE)) == ["suspect"]
        assert "BLANK COLUMN: jobs.suspect" in caplog.text
        assert f"all {ABOVE} rows" in caplog.text
        assert caplog.records[-1].levelno == logging.WARNING

    def test_all_blank_column_below_threshold_does_not_warn(self, caplog) -> None:
        assert check_blank_columns("jobs", _grid(BELOW)) == []
        assert "BLANK COLUMN" not in caplog.text

    def test_header_only_and_empty_grids_are_silent(self, caplog) -> None:
        assert check_blank_columns("jobs", []) == []
        assert check_blank_columns("jobs", [["st_id", "suspect"]]) == []
        assert "BLANK COLUMN" not in caplog.text


class TestPopulation:
    def test_one_populated_cell_is_enough_to_stay_quiet(self, caplog) -> None:
        grid = _grid(ABOVE)
        grid[-1][1] = "x"
        assert check_blank_columns("jobs", grid) == []
        assert "BLANK COLUMN" not in caplog.text

    def test_whitespace_only_cells_count_as_blank(self) -> None:
        assert check_blank_columns("jobs", _grid(ABOVE, second_column="   ")) == ["suspect"]

    def test_ragged_rows_do_not_raise(self, caplog) -> None:
        grid: list[list[str]] = [["st_id", "suspect"]] + [[str(i)] for i in range(ABOVE)]
        assert check_blank_columns("jobs", grid) == ["suspect"]

    def test_multiple_blank_columns_are_each_named(self, caplog) -> None:
        grid = [["a", "b", "c"]] + [["1", "", ""] for _ in range(ABOVE)]
        assert check_blank_columns("tabx", grid) == ["b", "c"]
        assert "tabx.b" in caplog.text
        assert "tabx.c" in caplog.text


class TestExemptions:
    def test_exempt_column_never_warns(self, caplog) -> None:
        grid = [["st_id", "parent_id"]] + [[str(i), ""] for i in range(ABOVE * 4)]
        assert check_blank_columns("pricebook.categories", grid) == []
        assert "BLANK COLUMN" not in caplog.text

    def test_exemption_is_scoped_to_its_own_tab(self, caplog) -> None:
        """`summary` is exempt on `jobs` only — it must not be exempt everywhere."""
        grid = [["st_id", "summary"]] + [[str(i), ""] for i in range(ABOVE)]
        assert check_blank_columns("jobs", grid) == []
        assert check_blank_columns("pricebook.services", grid) == ["summary"]

    def test_every_exemption_carries_a_written_reason(self) -> None:
        for tab, columns in ALL_BLANK_OK.items():
            for column, reason in columns.items():
                assert reason.strip(), f"{tab}.{column} is exempt with no reason given"

    def test_the_unverified_columns_are_not_exempt_anywhere(self) -> None:
        """The spellings this detector exists to catch must never be suppressed."""
        for column in ("job_number", "customer_phone", "customer_email", "st_technician_id"):
            for tab, columns in ALL_BLANK_OK.items():
                assert column not in columns, f"{tab}.{column} must never be exempt"


class TestJobNumberAcceptance:
    """Ticket 25's acceptance test: the original `job_number` bug, caught."""

    def test_jobs_grid_with_blank_job_number_is_caught(self, caplog) -> None:
        # Rows as `build_job_rows` would have produced them when the exporter read
        # `number` off a payload that spells it `jobNumber`: every other column
        # populated, `job_number` absent from the dict and therefore blank.
        rows = [
            {column: f"v{i}" for column in JOB_COLUMNS if column != "job_number"}
            for i in range(ABOVE)
        ]
        grid = [list(JOB_COLUMNS)] + [format_job_row(row) for row in rows]

        assert check_blank_columns("jobs", grid) == ["job_number"]
        assert "BLANK COLUMN: jobs.job_number" in caplog.text


class TestHookedIntoTheFeeds:
    def test_tab_guard_warns_and_still_writes_the_tab(self, caplog) -> None:
        """Pricebook and financial both run through `_TabGuard.attempt`."""
        grid = [["st_id", "code"]] + [[str(i), ""] for i in range(ABOVE)]
        new_meta_rows: list[MetaRow] = []
        guard = _TabGuard(
            label="pricebook",
            contract_version="pricebook.v1",
            meta_rows={},
            new_meta_rows=new_meta_rows,
            run_at="2026-09-15T00:00:00+00:00",
        )
        guard.attempt("pricebook.services", lambda: (grid, None))

        assert "BLANK COLUMN: pricebook.services.code" in caplog.text
        # A detector, not a guard: the tab is written, counted and _meta'd as usual.
        store = InMemorySheetsStore()
        guard.write(store, dry_run=False)
        assert store.tabs["pricebook.services"] == grid
        assert guard.row_counts["pricebook.services"] == ABOVE
        assert guard.failures == {}
        assert [row.feed for row in new_meta_rows] == ["pricebook.services"]

    def test_technicians_feed_warns_and_still_writes_the_tab(self, caplog) -> None:
        technicians = [
            {"id": i, "name": f"Tech {i}", "email": None, "active": True} for i in range(ABOVE)
        ]
        store = InMemorySheetsStore()
        new_meta_rows: list[MetaRow] = []
        with patch("st_exporter.run.fetch_technicians", return_value=technicians):
            row_count = _run_technicians_feed(
                Mock(),
                store,
                new_meta_rows=new_meta_rows,
                run_at="2026-09-15T00:00:00+00:00",
                dry_run=False,
            )

        assert "BLANK COLUMN: technicians.email" in caplog.text
        assert row_count == ABOVE
        assert store.tabs["technicians"][0] == list(TECHNICIAN_COLUMNS)
        assert len(store.tabs["technicians"]) == ABOVE + 1

    def test_a_broken_detector_cannot_fail_a_tab(self, caplog) -> None:
        grid = [["st_id", "code"]] + [[str(i), ""] for i in range(ABOVE)]
        with patch("st_exporter.blank_columns._scan", side_effect=RuntimeError("boom")):
            assert check_blank_columns("pricebook.services", grid) == []
        assert "BLANK COLUMN" not in caplog.text


@respx.mock
def test_jobs_feed_runs_the_detector_over_the_grid_it_writes(
    st_settings, exporter_settings
) -> None:
    """The wiring proof for the acceptance case: `jobs` is a checked tab."""
    today = date(2026, 9, 3)
    tenant_run1.register(
        st_settings.api_base,
        today_iso=today.isoformat(),
        far_past_iso=(today - timedelta(days=200)).isoformat(),
    )
    mock_auth_token(st_settings.auth_url)
    export_store = InMemorySheetsStore()

    with (
        patch(
            "st_exporter.run.datetime",
            **{"now.return_value": datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)},
        ),
        patch("st_exporter.run.check_blank_columns") as checked,
    ):
        run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )

    checked_tabs = {call.args[0] for call in checked.call_args_list}
    assert "jobs" in checked_tabs
    assert "technicians" in checked_tabs
    jobs_call = next(c for c in checked.call_args_list if c.args[0] == "jobs")
    assert jobs_call.args[1] == export_store.tabs["jobs"]

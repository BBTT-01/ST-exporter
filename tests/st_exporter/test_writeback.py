"""Tests for the jobs-tab write-back (ticket 17).

The load-bearing claim this file has to keep honest is not "the rows change" — it
is that a written-back row is **indistinguishable from the row the jobs feed would
have produced**. So the shape tests do not assert a hand-written expected row;
they assert equality with the committed `jobs.v2` contract fixture, the same file
the three consuming apps assert they can read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from st_exporter.format import JOB_COLUMNS, build_job_grid
from st_exporter.outbox.client import OutboxItem
from st_exporter.sheets import InMemorySheetsStore
from st_exporter.writeback import (
    AssignmentWriteBack,
    JobsWriteBack,
    apply_write_backs,
    write_back_for_item,
)
from tests.st_exporter.fixtures.contract_cases import job_rows

REPO_ROOT = Path(__file__).resolve().parents[2]
JOBS_FIXTURE = REPO_ROOT / "contracts" / "fixtures" / "jobs.v2" / "jobs.json"


def _fixture_grid() -> list[list[str]]:
    fixture = json.loads(JOBS_FIXTURE.read_text())
    return [list(fixture["columns"])] + [list(row) for row in fixture["rows"]]


def _at_fixture_width(grid: list[list[str]]) -> list[list[str]]:
    """``grid`` cut back to the columns the frozen `jobs.v2` fixture pins.

    The fixture is the bytes `jobs.v2` was RELEASED with and never moves again,
    so a column APPENDED to the jobs tab since — additive, and deliberately not a
    version bump (see ``st_exporter.contracts``) — has no counterpart in it.
    These tests are about the write-back reproducing the row SHAPE, not about the
    appended column's value, and every column the fixture does pin is still
    compared cell for cell.
    """
    width = len(_fixture_grid()[0])
    return [row[:width] for row in grid]


def _item(payload: dict[str, Any], kind: str = "assign_technician") -> OutboxItem:
    return OutboxItem(id="item-1", idempotency_key="key-1", kind=kind, payload=payload)


def _without(rows: list[dict[str, Any]], appointment_id: str, technician_id: str):
    return [
        row
        for row in rows
        if not (
            str(row.get("st_appointment_id")) == appointment_id
            and str(row.get("st_technician_id")) == technician_id
        )
    ]


class TestReadingAnItem:
    def test_a_set_diff_payload_is_read_as_a_set_diff(self) -> None:
        effect = write_back_for_item(
            "profitwizard",
            _item(
                {
                    "jobAppointmentId": 100,
                    "job_id": 1,
                    "technician_ids_to_add": [901],
                    "technician_ids_to_remove": [902],
                }
            ),
        )
        assert effect == AssignmentWriteBack(
            appointment_id="100", job_id="1", assigned=("901",), unassigned=("902",)
        )

    def test_a_bare_technician_list_is_an_assignment(self) -> None:
        effect = write_back_for_item(
            "profitwizard", _item({"appointmentId": 100, "technicianIds": [901, 902]})
        )
        assert effect == AssignmentWriteBack(appointment_id="100", assigned=("901", "902"))

    def test_an_operation_word_turns_a_bare_list_into_a_removal(self) -> None:
        effect = write_back_for_item(
            "profitwizard",
            _item({"appointmentId": 100, "technicianIds": [901], "operation": "unassign"}),
        )
        assert effect == AssignmentWriteBack(appointment_id="100", unassigned=("901",))

    def test_kinds_with_no_jobs_tab_effect_produce_nothing(self) -> None:
        lead = _item({"name": "Jane"}, kind="referral_lead")
        assert write_back_for_item("traderated", lead) is None
        assert (
            write_back_for_item(
                "traderated",
                _item({"servicetitan_job_id": "1", "rating": 5}, kind="technician_rating"),
            )
            is None
        )

    def test_an_unreadable_payload_is_logged_and_skipped_never_raised(self, caplog) -> None:
        """The ServiceTitan write already happened. Nothing about reading its
        payload may raise into the drain loop and put that in doubt."""
        with caplog.at_level("WARNING"):
            assert write_back_for_item("profitwizard", _item({"something": "else"})) is None
        assert "SUCCEEDED" in caplog.text
        assert "NOT failed" in caplog.text


class TestRowShapeMatchesTheFrozenFixture:
    def test_an_assigned_technician_reproduces_the_fixture_row_exactly(self) -> None:
        """The whole point of the ticket's shape constraint.

        Start from the world as it was before the assignment (technician 901 not
        on appointment 100), apply the write-back, and the grid must be the
        committed `jobs.v2` fixture — byte for byte, including row order.
        """
        before = _without(job_rows(), "100", "901")
        result = apply_write_backs(
            before, [AssignmentWriteBack(appointment_id="100", job_id="1", assigned=("901",))]
        )
        assert _at_fixture_width(build_job_grid(result.rows)) == _fixture_grid()
        assert result.rows_added == 1
        assert result.rows_removed == 0

    def test_an_unassigned_technician_removes_exactly_that_row(self) -> None:
        result = apply_write_backs(
            job_rows(), [AssignmentWriteBack(appointment_id="100", unassigned=("900",))]
        )
        expected = build_job_grid(_without(job_rows(), "100", "900"))
        assert build_job_grid(result.rows) == expected
        assert (result.rows_added, result.rows_removed) == (0, 1)

    def test_a_reassignment_is_one_removal_and_one_addition(self) -> None:
        result = apply_write_backs(
            job_rows(),
            [AssignmentWriteBack(appointment_id="100", assigned=("902",), unassigned=("900",))],
        )
        technicians = [
            row["st_technician_id"] for row in result.rows if str(row["st_appointment_id"]) == "100"
        ]
        # Newly assigned leads, which is the denormalise path's own order
        # (most-recently-assigned first).
        assert technicians == ["902", "901"]
        # Every other column came from the sibling row, not from a second builder.
        new_row = next(
            row
            for row in result.rows
            if str(row["st_appointment_id"]) == "100" and row["st_technician_id"] == "902"
        )
        sibling = next(
            row
            for row in job_rows()
            if str(row["st_appointment_id"]) == "100" and str(row["st_technician_id"]) == "901"
        )
        assert {key: value for key, value in new_row.items() if key != "st_technician_id"} == {
            key: value for key, value in sibling.items() if key != "st_technician_id"
        }

    def test_the_last_technician_leaving_keeps_the_blank_placeholder_row(self) -> None:
        """`build_job_rows` emits one blank-technician row for an appointment with
        no crew (its `[None]` fallback). An emptied appointment must look the same
        after a write-back, not vanish from the tab."""
        result = apply_write_backs(
            job_rows(),
            [AssignmentWriteBack(appointment_id="100", unassigned=("900", "901"))],
        )
        rows_100 = [row for row in result.rows if str(row["st_appointment_id"]) == "100"]
        assert len(rows_100) == 1
        assert rows_100[0]["st_technician_id"] is None
        grid = build_job_grid(result.rows)
        technician_column = JOB_COLUMNS.index("st_technician_id")
        assert [row[technician_column] for row in grid[1:] if row[1] == "100"] == [""]

    def test_the_columns_never_change(self) -> None:
        result = apply_write_backs(
            job_rows(), [AssignmentWriteBack(appointment_id="100", assigned=("999",))]
        )
        assert build_job_grid(result.rows)[0] == list(JOB_COLUMNS)
        assert all(len(row) == len(JOB_COLUMNS) for row in build_job_grid(result.rows))


class TestWhatIsNotApplied:
    def test_an_already_assigned_technician_changes_nothing(self) -> None:
        result = apply_write_backs(
            job_rows(), [AssignmentWriteBack(appointment_id="100", assigned=("901",))]
        )
        assert result.rows_changed == 0
        assert build_job_grid(result.rows) == build_job_grid(job_rows())

    def test_an_appointment_with_no_row_in_this_run_is_unmatched_not_invented(self) -> None:
        """Outside the window, or its job not in the raw cache yet. A row cannot be
        built without a customer, a location and a job type, and inventing one is
        exactly the second row-builder this module refuses to be."""
        effect = AssignmentWriteBack(appointment_id="99999", assigned=("901",))
        result = apply_write_backs(job_rows(), [effect])
        assert result.unmatched == [effect]
        assert result.rows_changed == 0
        assert build_job_grid(result.rows) == build_job_grid(job_rows())

    def test_a_job_id_that_disagrees_with_the_rows_is_refused(self, caplog) -> None:
        effect = AssignmentWriteBack(appointment_id="100", job_id="777", assigned=("902",))
        with caplog.at_level("WARNING"):
            result = apply_write_backs(job_rows(), [effect])
        assert result.unmatched == [effect]
        assert build_job_grid(result.rows) == build_job_grid(job_rows())
        assert "NOT changed" in caplog.text


class TestTheSheetWrite:
    def test_the_tab_is_replaced_whole_through_the_same_grid_path(self) -> None:
        """One `replace_grid` call with the complete grid — never a cell edit, never
        a clear-then-write. A reader mid-run sees the old tab or the new one."""
        store = InMemorySheetsStore()
        rows = _without(job_rows(), "100", "901")
        store.replace_grid("jobs", build_job_grid(rows))
        calls: list[tuple[str, int]] = []
        original = store.replace_grid

        def recording(tab_name: str, grid: list[list[str]]) -> None:
            calls.append((tab_name, len(grid)))
            original(tab_name, grid)

        store.replace_grid = recording  # type: ignore[method-assign]
        JobsWriteBack(store, rows).apply(
            [AssignmentWriteBack(appointment_id="100", assigned=("901",))]
        )
        assert calls == [("jobs", len(_fixture_grid()))]
        assert _at_fixture_width(store.tabs["jobs"]) == _fixture_grid()

    def test_nothing_is_written_when_nothing_changed(self) -> None:
        """A no-op write-back must not spend a Sheets write, nor risk failing one."""
        store = InMemorySheetsStore()
        JobsWriteBack(store, job_rows()).apply(
            [AssignmentWriteBack(appointment_id="404", assigned=("901",))]
        )
        assert "jobs" not in store.tabs

    def test_only_the_jobs_tab_is_ever_touched(self) -> None:
        """No `_meta`, no raw cache, no cursor — the tab and nothing else."""
        store = InMemorySheetsStore()
        store.replace_grid("_meta", [["feed"], ["jobs"]])
        before = [row[:] for row in store.tabs["_meta"]]
        JobsWriteBack(store, job_rows()).apply(
            [AssignmentWriteBack(appointment_id="100", assigned=("902",))]
        )
        assert set(store.tabs) == {"_meta", "jobs"}
        assert store.tabs["_meta"] == before

    def test_two_effects_on_one_appointment_compose(self) -> None:
        store = InMemorySheetsStore()
        handle = JobsWriteBack(store, job_rows())
        handle.apply([AssignmentWriteBack(appointment_id="100", unassigned=("900",))])
        handle.apply([AssignmentWriteBack(appointment_id="100", assigned=("903",))])
        technician_column = JOB_COLUMNS.index("st_technician_id")
        assert [row[technician_column] for row in store.tabs["jobs"][1:] if row[1] == "100"] == [
            "903",
            "901",
        ]

    def test_a_failing_sheet_write_propagates_to_the_caller_not_to_the_item(self) -> None:
        """`JobsWriteBack.apply` does not swallow — `cli.py` is the one place that
        decides a failed write-back is survivable, and it can only decide that
        because it runs after every item has already been reported succeeded."""
        store = InMemorySheetsStore()

        def boom(tab_name: str, grid: list[list[str]]) -> None:
            raise RuntimeError("sheets 429")

        store.replace_grid = boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            JobsWriteBack(store, job_rows()).apply(
                [AssignmentWriteBack(appointment_id="100", assigned=("902",))]
            )

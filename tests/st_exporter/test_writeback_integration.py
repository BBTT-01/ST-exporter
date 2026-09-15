"""The ticket's own done-when, end to end: an assignment the drain performed in
this run is in the jobs tab when this run ends.

Everything below the CLI is real — the real ``run_export`` against respx-mocked
ServiceTitan, the real ``drain_lanes``/``drain_outbox``, the real
``OutboxLedger``, and the real ``JobsWriteBack`` writing through the real
grid-replace path. Only Google Sheets is a double (there is no local gspread) and
only the product's HTTP outbox is a stand-in lane, because no product ships this
item kind yet: Profit Wizard's four writes are ticket 15.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from st_exporter.cli import main
from st_exporter.format import JOB_COLUMNS
from st_exporter.meta import MetaRow, MetaRowSet, build_meta_grid
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.drain import drain_lanes
from st_exporter.outbox.ledger import OutboxLedger
from st_exporter.run import run_export
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token
from tests.st_exporter.fixtures import tenant_run1

FIXED_TODAY = date(2026, 9, 3)
FIXED_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
_ARGV0 = "st-export"

_TECHNICIAN_COLUMN = JOB_COLUMNS.index("st_technician_id")


def _frozen_now():
    return patch("st_exporter.run.datetime", **{"now.return_value": FIXED_NOW})


def _assignment_item() -> OutboxItem:
    """Profit Wizard's dispatcher assigning technician 900 to appointment 100.

    The payload spelling is the one `lib/crm/assignment-push.ts` already builds
    for ServiceTitan's `assign-technicians` endpoint.
    """
    return OutboxItem(
        id="pw-1",
        idempotency_key="pw-key-1",
        kind="assign_technician",
        payload={"jobAppointmentId": 100, "job_id": 1, "technician_ids_to_add": [900]},
    )


def _lane(items, perform=None):
    lane = MagicMock()
    lane.product = "profitwizard"
    lane.claim.return_value = items
    lane.perform.side_effect = perform or (lambda client, item: "st-assigned")
    return lane


class _Harness:
    """One run of `st-export --feeds jobs,outbox` with real internals."""

    def __init__(self, lane, *, feeds: str = "jobs,outbox", forbid_jobs: bool = False) -> None:
        self.export_store = InMemorySheetsStore()
        self.raw_cache_store = InMemorySheetsStore()
        self.lane = lane
        self.feeds = feeds
        self.forbid_jobs = forbid_jobs
        self.drained: list = []

    def _export(self, st_settings, exporter_settings, **kwargs):
        return run_export(
            st_settings,
            exporter_settings,
            feeds=kwargs["feeds"],
            dry_run=kwargs["dry_run"],
            export_store=self.export_store,
            raw_cache_store=self.raw_cache_store,
        )

    def _drain(self, st_settings, exporter_settings):
        self.drained = drain_lanes(MagicMock(), [self.lane], OutboxLedger(self.raw_cache_store))
        return self.drained

    def run(self, monkeypatch, st_settings, exporter_settings) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", self.feeds])
        tenant_run1.register(
            st_settings.api_base,
            today_iso=FIXED_TODAY.isoformat(),
            far_past_iso="2026-01-01",
        )
        if self.forbid_jobs:
            # Registered AFTER the happy-path fixtures — respx keys routes by
            # pattern, so this replaces the jobs feed's first call. See
            # `test_scope_gating.TAB_FIRST_CALL`.
            respx.get(
                f"{st_settings.api_base.rstrip('/')}"
                f"/crm/v2/tenant/{st_settings.tenant_id}/export/customers"
            ).mock(return_value=httpx.Response(403, text="Scope validation failed"))
        mock_auth_token(st_settings.auth_url)
        with (
            _frozen_now(),
            patch("st_exporter.cli.load_settings", return_value=st_settings),
            patch("st_exporter.cli.ExporterSettings", return_value=exporter_settings),
            patch("st_exporter.cli.run_export", side_effect=self._export),
            patch("st_exporter.cli._drain_outboxes", side_effect=self._drain),
            pytest.raises(SystemExit) as exit_info,
        ):
            main()
        self.exit_code = exit_info.value.code

    def jobs_rows(self) -> list[list[str]]:
        return self.export_store.tabs["jobs"][1:]


@respx.mock
def test_an_assignment_performed_by_the_drain_is_in_the_jobs_tab_in_the_same_run(
    monkeypatch, capsys, st_settings, exporter_settings
) -> None:
    """THE DONE-WHEN. Without the write-back, appointment 100 has no technician in
    this run's tab — tenant_run1 returns no assignments at all — and the change
    would only surface on the NEXT jobs run, up to twelve minutes later."""
    harness = _Harness(_lane([_assignment_item()]))
    harness.run(monkeypatch, st_settings, exporter_settings)

    assert harness.exit_code == 0
    rows = harness.jobs_rows()
    assert [row[_TECHNICIAN_COLUMN] for row in rows] == ["900"]
    assert [row[:2] for row in rows] == [["1", "100"]]
    # Two rows changed, not one: the blank placeholder row went and a technician
    # row took its place, which is what `build_job_rows` itself would have done.
    assert "write_back_applied=1 write_back_rows_changed=2" in capsys.readouterr().out


@respx.mock
def test_no_cursor_moves_and_meta_is_written_exactly_once(
    monkeypatch, st_settings, exporter_settings
) -> None:
    """Cursors belong to the feed that fetched the data.

    The `_meta` grid after the write-back must be the one the jobs feed wrote,
    byte for byte — same cursor bundle, same `last_run_at` — and it must have been
    written exactly once in the whole run, before the drain, so no side lane can
    strand it.
    """
    writes: list[str] = []
    harness = _Harness(_lane([_assignment_item()]))
    original = harness.export_store.replace_grid

    def recording(tab_name: str, grid: list[list[str]]) -> None:
        writes.append(tab_name)
        original(tab_name, grid)

    harness.export_store.replace_grid = recording  # type: ignore[method-assign]
    harness.run(monkeypatch, st_settings, exporter_settings)

    # jobs (feed) -> _meta -> jobs (write-back). `_meta` once, and never last.
    assert writes == ["jobs", "_meta", "jobs"]
    meta = {row[0]: row for row in harness.export_store.tabs["_meta"][1:]}
    assert tenant_run1.APPOINTMENTS_CURSOR in meta["jobs"][2]
    assert tenant_run1.ASSIGNMENTS_CURSOR in meta["jobs"][2]
    # The write-back changed no row COUNT here (a blank-technician row became a
    # technician row), so even `row_count` still describes the tab exactly.
    assert meta["jobs"][3] == "1"


@respx.mock
def test_the_tab_is_never_half_written(monkeypatch, st_settings, exporter_settings) -> None:
    """Every write-back write is a complete grid through `replace_grid` — the same
    single-batchUpdate path the feed uses. A reader sees the pre-drain tab or the
    post-drain tab, never a tab mid-edit."""
    grids: list[list[list[str]]] = []
    harness = _Harness(_lane([_assignment_item()]))
    original = harness.export_store.replace_grid

    def recording(tab_name: str, grid: list[list[str]]) -> None:
        if tab_name == "jobs":
            grids.append([row[:] for row in grid])
        original(tab_name, grid)

    harness.export_store.replace_grid = recording  # type: ignore[method-assign]
    harness.run(monkeypatch, st_settings, exporter_settings)

    assert len(grids) == 2
    for grid in grids:
        assert grid[0] == list(JOB_COLUMNS)
        assert all(len(row) == len(JOB_COLUMNS) for row in grid)


@respx.mock
def test_a_failed_write_back_is_not_an_item_failure(
    monkeypatch, capsys, caplog, st_settings, exporter_settings
) -> None:
    """The constraint with teeth: reporting this item failed would make the app
    redeliver it and we would perform a SECOND real write into a contractor's
    ServiceTitan."""
    harness = _Harness(_lane([_assignment_item()]))
    harness.run_original_replace = harness.export_store.replace_grid

    calls = {"jobs": 0}

    def flaky(tab_name: str, grid: list[list[str]]) -> None:
        if tab_name == "jobs":
            calls["jobs"] += 1
            if calls["jobs"] == 2:  # the write-back, not the feed's own write
                raise RuntimeError("sheets 429")
        harness.run_original_replace(tab_name, grid)

    harness.export_store.replace_grid = flaky  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        harness.run(monkeypatch, st_settings, exporter_settings)

    # The run is green and the item stands as succeeded, reported once, success.
    assert harness.exit_code == 0
    summary = harness.drained[0].summary
    assert (summary.succeeded, summary.failed) == (1, 0)
    harness.lane.report_success.assert_called_once()
    harness.lane.report_failure.assert_not_called()
    # And the tab is simply the one the feed wrote — stale by one cycle, not broken.
    assert [row[_TECHNICIAN_COLUMN] for row in harness.jobs_rows()] == [""]
    out = capsys.readouterr().out
    assert "write_back_failed=1" in out
    assert "profitwizard_succeeded=1" in out
    assert "NOT failed" in caplog.text


@respx.mock
def test_a_drain_only_run_defers_the_write_back_and_touches_no_export_tab(
    monkeypatch, capsys, caplog, st_settings, exporter_settings
) -> None:
    """`--feeds outbox` makes no Export Store round-trip, and must keep making
    none: that is the stated justification for the outbox concurrency lock being
    separate from the export one. The change is live in ServiceTitan either way;
    the next jobs run exports it."""
    harness = _Harness(_lane([_assignment_item()]), feeds="outbox")
    with caplog.at_level(logging.WARNING):
        harness.run(monkeypatch, st_settings, exporter_settings)

    assert harness.exit_code == 0
    assert harness.export_store.tabs == {}
    assert harness.drained[0].summary.succeeded == 1
    out = capsys.readouterr().out
    assert "write_back_deferred=1" in out
    assert "jobs,outbox" in caplog.text


@respx.mock
def test_an_unassignment_performed_by_the_drain_removes_the_row_in_the_same_run(
    monkeypatch, st_settings, exporter_settings
) -> None:
    item = OutboxItem(
        id="pw-2",
        idempotency_key="pw-key-2",
        kind="assign_technician",
        payload={
            "jobAppointmentId": 100,
            "technician_ids_to_add": [901],
            "technician_ids_to_remove": [900],
        },
    )
    harness = _Harness(_lane([item]))
    harness.run(monkeypatch, st_settings, exporter_settings)

    assert [row[_TECHNICIAN_COLUMN] for row in harness.jobs_rows()] == ["901"]


@respx.mock
def test_a_jobs_tab_never_granted_is_not_resurrected_by_the_write_back(
    monkeypatch, capsys, caplog, st_settings, exporter_settings
) -> None:
    """A quiet skip means "no tab", and the write-back may not put one there.

    ServiceTitan refuses this tenant the `jobs` feed and there is no `_meta`
    evidence of a prior run, so `scopes.py` classifies it "never granted": the
    run stays green, writes no `jobs` tab, and says nothing loud. The drain still
    performs its assignment — the outbox lane is a different app and a different
    permission — so this run reaches the write-back holding an effect and no
    grid. If the write-back had any way of building rows of its own, this is the
    run in which it would invent a whole `jobs` tab for a contractor who does not
    have the entity, out of two ids. It is built only in the jobs feed's success
    branch, so there is no handle and nothing to apply.
    """
    harness = _Harness(_lane([_assignment_item()]), forbid_jobs=True)
    with caplog.at_level(logging.WARNING):
        harness.run(monkeypatch, st_settings, exporter_settings)

    # Green and quiet: a tab the contractor never bought is not a failure.
    assert harness.exit_code == 0
    assert "jobs" not in harness.export_store.tabs
    # The write happened in ServiceTitan and the item stands succeeded.
    assert harness.drained[0].summary.succeeded == 1
    harness.lane.report_failure.assert_not_called()
    out = capsys.readouterr().out
    assert "write_back_applied" not in out
    assert "write_back_scope_denied=1" in out
    # And NOT the drain-only advice: changing `feeds:` would fix nothing here.
    assert "jobs,outbox" not in caplog.text


@respx.mock
def test_a_revoked_jobs_tab_is_left_exactly_as_the_last_good_run_left_it(
    monkeypatch, capsys, st_settings, exporter_settings
) -> None:
    """A revoked tab is frozen evidence, not a tab to overwrite.

    There IS a prior successful run in `_meta`, so the same 403 is a revocation:
    loud, non-zero exit, and the tab and its `_meta` row are left exactly as the
    last good run left them. The write-back must not be the thing that edits
    them — it would replace a preserved tab with rows this run never fetched,
    and leave `_meta` describing a tab that no longer matches it.
    """
    stale_tab = [list(JOB_COLUMNS), ["1", "100", *[""] * (len(JOB_COLUMNS) - 2)]]
    harness = _Harness(_lane([_assignment_item()]), forbid_jobs=True)
    harness.export_store.replace_grid("jobs", stale_tab)
    meta = MetaRowSet()
    meta.add(MetaRow(feed="jobs", last_run_at="2026-09-02T12:00:00+00:00", row_count=1))
    harness.export_store.replace_grid("_meta", build_meta_grid(meta))
    before_meta = [row[:] for row in harness.export_store.tabs["_meta"]]

    harness.run(monkeypatch, st_settings, exporter_settings)

    # Loud: the permission was taken away, so the run is red.
    assert harness.exit_code == 1
    # And untouched, by the feed and by the write-back alike.
    assert harness.export_store.tabs["jobs"] == stale_tab
    assert harness.export_store.tabs["_meta"] == before_meta
    assert harness.drained[0].summary.succeeded == 1
    out = capsys.readouterr().out
    assert "write_back_applied" not in out
    assert "write_back_scope_denied=1" in out

"""Ticket 21: a failing feed must not discard a committed feed's cursor.

The defect these tests pin down is silent and self-repeating. `_meta` — which
carries the cursors — is written ONCE, after every feed has run. The `jobs` feed
commits five raw-cache grids and the `jobs` tab and only then appends its cursor
to the pending `_meta` rows, so anything that threw between that and the `_meta`
write (an unguarded `fetch_technicians` raising 403 on a missing Settings →
Technicians permission, or 400 on the unverified `active=Any` parameter) left the
tab freshly written on disk and the cursor exactly where it was. Every subsequent
run then re-drained every change feed from the beginning, forever, and nothing
anywhere said so: it presented as a slow exporter, not as an error.

So there are two halves to hold, and they pull in opposite directions:

* a committed feed must KEEP its cursor when a later feed dies, and
* a cursor must never advance past data that was not written.

The second is the stricter one — a re-drain is slow but correct, while a cursor
that leads the data skips a window of changes nothing will ever fetch again — so
the cursor is always the trailing edge. Both directions are tested here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
import respx

from st_cli.exceptions import APIError, STCLIError
from st_exporter.meta import CursorBundle, parse_meta_grid
from st_exporter.run import run_export
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token
from tests.st_exporter.fixtures import tenant_run1, tenant_run2

FIXED_TODAY = date(2026, 9, 3)
FIXED_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
# Run 2 is frozen an hour later than run 1, so `last_run_at` alone distinguishes a
# `_meta` row written THIS run from one carried forward from the previous one.
FIXED_NOW_RUN2 = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)


def _frozen_now(now: datetime = FIXED_NOW):
    return patch("st_exporter.run.datetime", **{"now.return_value": now})


def _technicians_url(api_base: str) -> str:
    return f"{api_base}/settings/v2/tenant/{tenant_run1.TENANT_ID}/technicians"


def _first_run(st_settings, exporter_settings):
    """Run 1, fully successful, so there is a real cursor to lose in run 2."""
    export_store = InMemorySheetsStore()
    raw_cache_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)
    tenant_run1.register(
        st_settings.api_base,
        today_iso=FIXED_TODAY.isoformat(),
        far_past_iso=(FIXED_TODAY - timedelta(days=200)).isoformat(),
    )
    with _frozen_now():
        run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert CursorBundle.decode(meta["jobs"].last_cursor).get("jobs") == tenant_run1.JOBS_CURSOR
    return export_store, raw_cache_store


def _second_run_with_broken_technicians(
    st_settings, exporter_settings, export_store, raw_cache_store, response: httpx.Response
):
    """Run 2: the jobs feed succeeds, the technicians endpoint returns ``response``.

    ``tenant_run2``'s own routes assert that each delta fetch carries exactly run
    1's cursor, so a run that had silently restarted from the beginning would fail
    inside the fixture rather than here.
    """
    tenant_run2.register(st_settings.api_base, today_iso=FIXED_TODAY.isoformat())
    respx.get(_technicians_url(st_settings.api_base)).mock(return_value=response)
    with _frozen_now(FIXED_NOW_RUN2):
        return run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )


@respx.mock
@pytest.mark.parametrize(
    ("status", "ledger"),
    [
        # A Settings → Technicians permission that has been TAKEN AWAY. Run 1
        # wrote the tab, so `scopes.py` classifies this 403 as `revoked` rather
        # than as a feed failure — a red `::error` and a non-zero exit, which is
        # louder than this guard's own warning, not quieter. The tab is still not
        # written and its `_meta` row is still carried forward, so everything
        # this ticket is about is unchanged by that classification.
        (403, "scope_revoked"),
        # The unverified `active=Any` parameter being rejected. The fallback in
        # `fetch_technicians` retries without the parameter; this response rejects
        # that too, so the feed genuinely fails and the guard has to hold. Not a
        # 403, so it never reaches the scope ledger: an outage is not a purchase.
        (400, "feed_failures"),
    ],
)
def test_a_broken_technicians_feed_leaves_the_jobs_cursor_advanced(
    st_settings, exporter_settings, status: int, ledger: str
) -> None:
    """The ticket, exactly: jobs commits, technicians raises, cursors must advance."""
    export_store, raw_cache_store = _first_run(st_settings, exporter_settings)

    summary = _second_run_with_broken_technicians(
        st_settings,
        exporter_settings,
        export_store,
        raw_cache_store,
        httpx.Response(status, text="no."),
    )

    meta = parse_meta_grid(export_store.tabs["_meta"])

    # 1. The jobs tab was written this run, and so was its cursor. Before the fix
    #    `_meta` was never written at all and this decoded to run 1's token.
    bundle = CursorBundle.decode(meta["jobs"].last_cursor)
    assert bundle.get("jobs") == tenant_run2.JOBS_CURSOR
    assert bundle.get("appointments") == tenant_run2.APPOINTMENTS_CURSOR
    assert bundle.get("customers") == tenant_run2.CUSTOMERS_CURSOR
    assert meta["jobs"].last_run_at == FIXED_NOW_RUN2.isoformat()
    assert meta["jobs"].row_count == 2
    assert {row[0] for row in export_store.tabs["jobs"][1:]} == {"1", "2"}

    # 2. The technicians row is carried forward untouched — its `last_run_at`
    #    still names the run that last genuinely refreshed the tab, and the tab
    #    keeps run 1's contents rather than being emptied.
    assert meta["technicians"].last_run_at == FIXED_NOW.isoformat()
    assert meta["technicians"].row_count == 1
    assert len(export_store.tabs["technicians"]) == 2

    # 3. The run reports the failure rather than exiting 0 looking healthy —
    #    through whichever of the two channels fits what actually happened.
    if ledger == "scope_revoked":
        assert summary.feed_failures is None
        assert set(summary.scope_revoked) == {"technicians"}
        assert str(status) in summary.scope_revoked["technicians"]
    else:
        assert not summary.scope_revoked and not summary.scope_not_granted
        assert summary.feed_failures is not None
        assert set(summary.feed_failures) == {"technicians"}
        assert str(status) in summary.feed_failures["technicians"]
    assert summary.jobs_row_count == 2


@respx.mock
def test_a_broken_technicians_feed_is_announced_where_a_human_will_see_it(
    st_settings, exporter_settings, monkeypatch, tmp_path, capsys, caplog
) -> None:
    """A green run's log is a log nobody opens — say it on the run itself.

    The sharpest point of the ticket is "nothing in the logs says this is
    happening". An Actions annotation plus a step-summary line is how the
    blank-column detector already answers that, and it is what a cursor failing to
    advance now gets too.

    The failure here is a 400 (`active=Any` rejected, and rejected again without
    it) rather than a 403: a 403 is a permission answer now, and `scopes.py`
    announces that one itself at `::error` — see `test_scope_gating.py`. This
    channel is the one every OTHER way a feed can die still needs.
    """
    export_store, raw_cache_store = _first_run(st_settings, exporter_settings)

    summary_file = tmp_path / "step_summary.md"
    summary_file.write_text("")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    with caplog.at_level("WARNING", logger="st_exporter"):
        _second_run_with_broken_technicians(
            st_settings,
            exporter_settings,
            export_store,
            raw_cache_store,
            httpx.Response(400, text="Unknown parameter 'active'"),
        )

    annotation = capsys.readouterr().out
    assert "::warning title=Feed failed::" in annotation
    assert "technicians" in annotation

    step_summary = summary_file.read_text()
    assert "Feed failed" in step_summary
    assert "technicians" in step_summary

    # And the log still says it, naming the consequence rather than only the error.
    assert any("FEED FAILED: technicians" in record.message for record in caplog.records)


@respx.mock
def test_a_failing_jobs_feed_never_advances_its_own_cursor(st_settings, exporter_settings) -> None:
    """The other direction: a cursor must not lead the data it describes.

    A re-drain is slow but recoverable; a cursor that advanced past a window that
    was never written loses those changes permanently, because nothing fetches
    them again. So the jobs cursor is appended only once the tab is on disk —
    here the tab write itself fails, and the old cursor must survive intact.
    """
    export_store, raw_cache_store = _first_run(st_settings, exporter_settings)
    before = [row[:] for row in export_store.tabs["jobs"]]

    class FailsOnJobsTab(InMemorySheetsStore):
        def replace_grid(self, tab_name: str, grid: list[list[str]]) -> None:
            if tab_name == "jobs":
                raise STCLIError("Sheets refused the jobs tab")
            super().replace_grid(tab_name, grid)

    broken = FailsOnJobsTab()
    for name, grid in export_store.tabs.items():
        broken.tabs[name] = [row[:] for row in grid]

    tenant_run2.register(st_settings.api_base, today_iso=FIXED_TODAY.isoformat())
    with _frozen_now(FIXED_NOW_RUN2):
        summary = run_export(
            st_settings,
            exporter_settings,
            export_store=broken,
            raw_cache_store=raw_cache_store,
        )

    # Exactly one row per feed, and the `jobs` one still carries run 1's cursor.
    # The row count matters as much as the cursor: a feed that appended a row and
    # then threw must leave none behind, or `_meta` carries two rows for `jobs`
    # and which cursor a consumer sees depends on parse order.
    grid = broken.tabs["_meta"]
    feed_names = [row[0] for row in grid[1:]]
    assert len(feed_names) == len(set(feed_names)) == 2

    meta = parse_meta_grid(grid)
    assert CursorBundle.decode(meta["jobs"].last_cursor).get("jobs") == tenant_run1.JOBS_CURSOR
    assert meta["jobs"].last_run_at == FIXED_NOW.isoformat()
    assert broken.tabs["jobs"] == before
    assert summary.feed_failures is not None and "jobs" in summary.feed_failures
    # The technicians feed still ran and still got its own fresh `_meta` row —
    # per-feed isolation cuts both ways.
    assert meta["technicians"].last_run_at == FIXED_NOW_RUN2.isoformat()


@respx.mock
def test_a_403_on_job_types_degrades_instead_of_killing_the_whole_jobs_feed(
    st_settings, exporter_settings
) -> None:
    """`job_types` / `business_units` are a denormalise FALLBACK, not a dependency.

    Losing them costs at most a blank `job_type` column for jobs that carry only
    an id. Aborting the jobs feed for them — which is what an unguarded fetch did
    — costs every job row plus the cursor.
    """
    export_store = InMemorySheetsStore()
    raw_cache_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)
    tenant_run1.register(
        st_settings.api_base,
        today_iso=FIXED_TODAY.isoformat(),
        far_past_iso=(FIXED_TODAY - timedelta(days=200)).isoformat(),
    )
    respx.get(f"{st_settings.api_base}/jpm/v2/tenant/{tenant_run1.TENANT_ID}/job-types").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )

    with _frozen_now():
        summary = run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )

    assert summary.jobs_row_count == 1
    assert summary.feed_failures is None
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert CursorBundle.decode(meta["jobs"].last_cursor).get("jobs") == tenant_run1.JOBS_CURSOR


class TestActiveAnyFallback:
    """`active=Any` is an unverified guess (KNOWN_UNVERIFIED.md) that can 400.

    It is NOT removed here: dropping it would silently make the tab active-only,
    and that guess is no better founded than the one it replaces. Instead the
    wrong guess is made survivable — a 400, and only a 400, re-fetches without the
    parameter and says loudly what the tab may now be missing.
    """

    @respx.mock
    def test_a_400_retries_without_the_parameter_and_still_writes_the_tab(
        self, st_settings, exporter_settings, monkeypatch, tmp_path, capsys
    ) -> None:
        export_store = InMemorySheetsStore()
        raw_cache_store = InMemorySheetsStore()
        mock_auth_token(st_settings.auth_url)
        tenant_run1.register(
            st_settings.api_base,
            today_iso=FIXED_TODAY.isoformat(),
            far_past_iso=(FIXED_TODAY - timedelta(days=200)).isoformat(),
        )

        seen: list[str | None] = []

        def technicians(request: httpx.Request) -> httpx.Response:
            active = request.url.params.get("active")
            seen.append(active)
            if active is not None:
                return httpx.Response(400, text="Unknown parameter 'active'")
            return httpx.Response(200, json={"data": [tenant_run1.TECHNICIAN_1], "hasMore": False})

        respx.get(_technicians_url(st_settings.api_base)).mock(side_effect=technicians)

        summary_file = tmp_path / "step_summary.md"
        summary_file.write_text("")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

        with _frozen_now():
            summary = run_export(
                st_settings,
                exporter_settings,
                export_store=export_store,
                raw_cache_store=raw_cache_store,
            )

        assert seen == ["Any", None]
        assert summary.technicians_row_count == 1
        assert summary.feed_failures is None
        assert "active=Any rejected" in capsys.readouterr().out
        assert "active=Any" in summary_file.read_text()

    @respx.mock
    def test_a_403_is_re_raised_rather_than_retried_without_the_parameter(
        self, st_settings, exporter_settings
    ) -> None:
        """Only a 400 means "I don't accept this filter".

        A 403 is about the request's fate, not the parameter, and retrying without
        it would just burn a second call and muddy the reason in the annotation.
        """
        from st_exporter.feeds.reference import fetch_technicians

        calls: list[httpx.Request] = []

        def technicians(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(403, text="Forbidden")

        mock_auth_token(st_settings.auth_url)
        respx.get(_technicians_url(st_settings.api_base)).mock(side_effect=technicians)

        from st_cli.client import ServiceTitanClient

        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(APIError) as caught:
                fetch_technicians(client)
        finally:
            client.close()

        assert caught.value.status_code == 403
        assert len(calls) == 1
        assert calls[0].url.params.get("active") == "Any"

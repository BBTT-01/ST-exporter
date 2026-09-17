"""Tests for the `st-export` CLI entrypoint: flag wiring and error handling.

``main()`` wraps ``typer.run(run_once)``, which parses ``sys.argv`` directly and
always raises ``SystemExit`` (click's standalone mode) — even on success — so
every test patches ``sys.argv`` and calls ``main()`` directly rather than going
through Typer's ``CliRunner`` (which expects a Click/Typer app object, not a
plain wrapper function).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pydantic
import pytest

from st_cli.exceptions import ConfigError
from st_exporter.cli import _summary_line, main
from st_exporter.images.upload import ImageUploadSummary
from st_exporter.outbox.drain import DrainSummary, LaneOutcome
from st_exporter.run import DEFAULT_FEEDS, ExportSummary

_ARGV0 = "st-export"


def _summary(**overrides) -> ExportSummary:
    defaults = dict(jobs_row_count=3, technicians_row_count=2, skipped_no_job=0, dry_run=False)
    defaults.update(overrides)
    return ExportSummary(**defaults)


class TestSuccessPath:
    def test_echoes_summary_and_exits_zero(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0])
        with (
            patch("st_exporter.cli.load_settings", return_value="fake-st-settings"),
            patch("st_exporter.cli.ExporterSettings", return_value="fake-exporter-settings"),
            patch(
                "st_exporter.cli.TradeRatedSettings",
                return_value=MagicMock(configured=False, images_configured=False),
            ),
            patch("st_exporter.cli.run_export", return_value=_summary()) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        assert "jobs=3 technicians=2 skipped_no_job=0 dry_run=False" in capsys.readouterr().out
        mock_run.assert_called_once_with(
            "fake-st-settings",
            "fake-exporter-settings",
            feeds=DEFAULT_FEEDS,
            dry_run=False,
        )

    def test_dry_run_flag_is_passed_through(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--dry-run"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch(
                "st_exporter.cli.TradeRatedSettings",
                return_value=MagicMock(configured=False, images_configured=False),
            ),
            patch("st_exporter.cli.run_export", return_value=_summary(dry_run=True)) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_run.assert_called_once_with("s", "e", feeds=DEFAULT_FEEDS, dry_run=True)

    def test_pricebook_noop_flag_is_gone(self, monkeypatch, capsys) -> None:
        """`--pricebook` was a deliberate no-op; ticket 06 replaced it with a real
        feed selected by `--feeds pricebook`, so the flag must now be rejected
        rather than silently accepted and ignored."""
        monkeypatch.setattr("sys.argv", [_ARGV0, "--pricebook"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch(
                "st_exporter.cli.TradeRatedSettings",
                return_value=MagicMock(configured=False, images_configured=False),
            ),
            patch("st_exporter.cli.run_export", return_value=_summary()) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        mock_run.assert_not_called()

    def test_pricebook_row_counts_are_echoed_when_the_feed_ran(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "pricebook"])
        counts = {
            "pricebook.services": 2,
            "pricebook.equipment": 1,
            "pricebook.materials": 3,
            "pricebook.categories": 4,
        }
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch(
                "st_exporter.cli.TradeRatedSettings",
                return_value=MagicMock(configured=False, images_configured=False),
            ),
            patch(
                "st_exporter.cli.run_export",
                return_value=_summary(pricebook_row_counts=counts),
            ) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "pricebook_services=2" in out
        assert "pricebook_categories=4" in out
        mock_run.assert_called_once_with("s", "e", feeds=frozenset({"pricebook"}), dry_run=False)

    def test_a_failed_pricebook_tab_is_named_in_the_run_output(self, monkeypatch, capsys) -> None:
        # Not only in the log: a tab left at last run's contents is a fact the
        # reader of the catalogue needs, and must not take digging to find.
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "pricebook"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch(
                "st_exporter.cli.TradeRatedSettings",
                return_value=MagicMock(configured=False, images_configured=False),
            ),
            patch(
                "st_exporter.cli.run_export",
                return_value=_summary(
                    pricebook_row_counts={"pricebook.services": 2},
                    pricebook_failures={"pricebook.equipment": "HTTP 500: boom"},
                ),
            ),
            pytest.raises(SystemExit),
        ):
            main()

        assert "pricebook_failed=pricebook_equipment" in capsys.readouterr().out


class TestFeedsFlag:
    def test_feeds_flag_is_parsed_and_passed_through(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_run.assert_called_once_with("s", "e", feeds=frozenset({"jobs"}), dry_run=False)

    def test_invalid_feeds_value_prints_clean_error_and_exits_one(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "not-a-feed"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        assert "Error:" in capsys.readouterr().err


class TestErrorHandling:
    def test_st_cli_error_from_run_export_prints_clean_error_and_exits_one(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", side_effect=ConfigError("bad config")),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        assert "Error: bad config" in capsys.readouterr().err

    def test_validation_error_from_exporter_settings_exits_one_not_a_traceback(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0])
        validation_error = pydantic.ValidationError.from_exception_data("ExporterSettings", [])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", side_effect=validation_error),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        assert "Error:" in capsys.readouterr().err


class TestOutboxDrain:
    """The drain is OPT-IN on `--feeds outbox`.

    This is the structural half of ticket 12's "exactly one workflow job" rule:
    before it, the exporter drained on ANY invocation whose secrets happened to
    be present, so a second job given the same secrets doubled the drain rate
    and put a second writer on the `_outbox_ledger` tab. A comment in the caller
    workflow was the only thing preventing it, and that comment was stripped
    from a live connector repo.
    """

    def test_does_not_drain_without_the_outbox_feed_even_with_secrets(self, monkeypatch) -> None:
        """Secrets alone can no longer cause a drain. THIS is what makes adding
        the secrets to a second workflow job inert rather than harmful."""
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "t")
        monkeypatch.setenv("TRADERATED_OUTBOX_URL", "https://tr.test")
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs,technicians"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli._drain_outboxes") as mock_drain,
            pytest.raises(SystemExit),
        ):
            main()
        mock_drain.assert_not_called()

    def test_an_undrained_configured_lane_is_warned_about_loudly(self, monkeypatch, caplog) -> None:
        """The migration gap made un-missable: a connector that bumps to this
        version without adding `outbox` to one job would otherwise just stop
        draining, on green runs, forever."""
        monkeypatch.setenv("TRUEQUOTE_MACHINE_TOKEN", "t")
        monkeypatch.setenv("TRUEQUOTE_OUTBOX_URL", "https://tq.test")
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs"])
        with (
            caplog.at_level(logging.WARNING),
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            pytest.raises(SystemExit),
        ):
            main()
        assert "truequote" in caplog.text
        assert "`outbox` feed" in caplog.text

    def test_no_warning_when_no_product_is_configured(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs"])
        with (
            caplog.at_level(logging.WARNING),
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            pytest.raises(SystemExit),
        ):
            main()
        assert "NOTHING was drained" not in caplog.text

    def test_drains_when_the_outbox_feed_is_requested(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs,outbox"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli._drain_outboxes", return_value=[]) as mock_drain,
            pytest.raises(SystemExit),
        ):
            main()
        mock_drain.assert_called_once_with("s", "e")

    def test_outbox_only_run_skips_the_export_half_entirely(self, monkeypatch) -> None:
        """A dedicated drain job must not pay for a Sheets round-trip it does not
        need — `--feeds outbox` fetches nothing and writes no tab."""
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "outbox"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export") as mock_export,
            patch("st_exporter.cli._drain_outboxes", return_value=[]) as mock_drain,
            pytest.raises(SystemExit),
        ):
            main()
        mock_export.assert_not_called()
        mock_drain.assert_called_once()

    def test_per_lane_counters_are_echoed_with_the_product_name(self, monkeypatch, capsys) -> None:
        outcomes = [
            LaneOutcome(product="traderated", summary=DrainSummary(2, 1, 1, 0)),
            LaneOutcome(product="truequote", summary=DrainSummary(3, 3, 0, 0)),
        ]
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs,outbox"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli._drain_outboxes", return_value=outcomes),
            pytest.raises(SystemExit),
        ):
            main()
        out = capsys.readouterr().out
        assert "traderated_claimed=2 traderated_succeeded=1 traderated_failed=1" in out
        assert "truequote_claimed=3 truequote_succeeded=3" in out

    def test_a_dead_lane_is_named_in_the_output_not_hidden(self, monkeypatch, capsys) -> None:
        outcomes = [
            LaneOutcome(product="traderated", error="connection refused"),
            LaneOutcome(product="truequote", summary=DrainSummary(1, 1, 0, 0)),
        ]
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs,outbox"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli._drain_outboxes", return_value=outcomes),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "traderated_lane_error=1" in out
        assert "truequote_succeeded=1" in out

    def test_outbox_failure_does_not_fail_the_run_or_print_a_traceback(
        self, monkeypatch, capsys
    ) -> None:
        """The export's Sheets writes are already committed by the time the drain
        runs, so an outbox error must exit 0 with the export summary — not a raw
        httpx traceback (which main()'s STCLIError/ValidationError handler would
        not catch)."""
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs,technicians,outbox"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch(
                "st_exporter.cli._drain_outboxes",
                side_effect=httpx.ConnectError("outbox unreachable"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "jobs=3 technicians=2" in captured.out
        assert "_claimed" not in captured.out
        assert "Traceback" not in captured.err

    def test_outbox_keyboard_interrupt_is_not_swallowed(self, monkeypatch, capsys) -> None:
        """`except Exception` must let KeyboardInterrupt/SystemExit through — it
        reaches click, which turns it into its standard abort exit code 130, not
        the exit-0-with-a-summary of the swallowed-error path."""
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs,technicians,outbox"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli._drain_outboxes", side_effect=KeyboardInterrupt),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 130
        assert "jobs=3" not in capsys.readouterr().out

    def test_skips_outbox_drain_on_dry_run_even_when_requested(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--dry-run", "--feeds", "jobs,outbox"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary(dry_run=True)),
            patch("st_exporter.cli._drain_outboxes") as mock_drain,
            pytest.raises(SystemExit),
        ):
            main()
        mock_drain.assert_not_called()


class TestTheSummaryLineNamesTheImagePass:
    """`stopped` must reach the run's OUTPUT, not only the log.

    A pass that aborted has not looked at the rest of the catalogue, so
    `images_uploaded=0 images_failed=0` reads as "nothing to do" — a green line
    over an image sync that silently stopped. Same rule as `pricebook_failed`
    and `financial_failed`: a fact that changes what the counters mean is named
    where the run is read.
    """

    def _summary(self, images: ImageUploadSummary) -> str:
        return _summary_line(
            ExportSummary(
                jobs_row_count=1,
                technicians_row_count=1,
                skipped_no_job=0,
                dry_run=False,
                images=images,
            ),
            [],
        )

    def test_a_clean_pass_says_stopped_no(self) -> None:
        line = self._summary(ImageUploadSummary(considered=3, uploaded=3))
        assert "images_stopped=no" in line

    def test_a_stopped_pass_names_the_reason(self) -> None:
        line = self._summary(
            ImageUploadSummary(stopped="image ledger flush failed: RuntimeError: Sheets 429")
        )
        assert "images_stopped=image ledger flush failed" in line
        # ...and it must not read like a clean run.
        assert "images_stopped=no" not in line

    def test_too_large_and_unsupported_are_surfaced_too(self) -> None:
        line = self._summary(ImageUploadSummary(too_large=2, unsupported=5))
        assert "images_too_large=2" in line
        assert "images_unsupported=5" in line


class TestAnUnwritableLedgerIsNotASilentlyGreenRun:
    """The failure the cross-lane fix introduced one file over.

    Skipping a lane when the shared ledger is unwritable is correct — it is what
    stops each remaining lane performing one more unledgered ServiceTitan write.
    But a skipped lane claimed, performed and reported nothing and returned
    `DrainSummary(0, 0, 0, 0)` with `error=None`: byte-identical, in the summary
    line, to a lane whose queue was empty. No annotation, and `cli.py` exited
    non-zero only for a config error.

    So a protected, deleted or quota-hit `_outbox_ledger` tab meant two products'
    bookings and leads queued INDEFINITELY while every Actions run stayed green —
    the same "invisible warning in a green run" the blank-column annotation exists
    to prevent.
    """

    def _outcomes(self) -> list[LaneOutcome]:
        return [
            LaneOutcome(
                product="traderated",
                summary=DrainSummary(1, 1, 0, 0, ledger_unwritable=True),
            ),
            LaneOutcome(
                product="truequote",
                summary=DrainSummary(
                    0, 0, 0, 0, skipped_ledger_unwritable=True, ledger_unwritable=True
                ),
            ),
            LaneOutcome(
                product="profitwizard",
                summary=DrainSummary(
                    0, 0, 0, 0, skipped_ledger_unwritable=True, ledger_unwritable=True
                ),
            ),
        ]

    def _run(self, monkeypatch, outcomes):  # type: ignore[no-untyped-def]
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs,outbox"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli._drain_outboxes", return_value=outcomes),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        return exc_info.value.code

    def test_the_run_exits_non_zero(self, monkeypatch, capsys) -> None:
        """The drain is the last thing the run does and every export write is
        already committed to the Sheet, so failing here costs nothing — and it is
        the only channel a queue going nowhere has."""
        assert self._run(monkeypatch, self._outcomes()) != 0

    def test_the_summary_line_distinguishes_a_skipped_lane_from_an_idle_one(
        self, monkeypatch, capsys
    ) -> None:
        self._run(monkeypatch, self._outcomes())
        out = capsys.readouterr().out
        assert "ledger_unwritable=1" in out
        assert "truequote_skipped=1" in out
        assert "profitwizard_skipped=1" in out
        # The lane that DID work is not marked skipped.
        assert "traderated_skipped=1" not in out

    def test_an_idle_lane_produces_neither_marker(self, monkeypatch, capsys) -> None:
        """The counters are identical to the starved case, so the markers are the
        only thing carrying the difference — and they must not cry wolf."""
        idle = [
            LaneOutcome(product="truequote", summary=DrainSummary(0, 0, 0, 0)),
            LaneOutcome(product="profitwizard", summary=DrainSummary(0, 0, 0, 0)),
        ]
        assert self._run(monkeypatch, idle) == 0
        out = capsys.readouterr().out
        assert "truequote_claimed=0 truequote_succeeded=0" in out
        assert "_skipped=1" not in out
        assert "ledger_unwritable" not in out

    def test_a_single_lane_run_reports_it_too(self, monkeypatch, capsys) -> None:
        """There is no later lane to be skipped and carry the news, so the lane
        that DISCOVERS the unwritable ledger has to."""
        outcomes = [
            LaneOutcome(
                product="traderated",
                summary=DrainSummary(2, 1, 0, 0, ledger_unwritable=True),
            )
        ]
        assert self._run(monkeypatch, outcomes) != 0
        assert "ledger_unwritable=1" in capsys.readouterr().out

    def test_it_is_announced_to_actions_as_an_error_not_a_warning(
        self, monkeypatch, capsys
    ) -> None:
        """A red annotation, because nothing here is a suspicion: work is not
        getting done and will not get done on the next run either."""
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        self._run(monkeypatch, self._outcomes())
        out = capsys.readouterr().out
        annotations = [line for line in out.splitlines() if line.startswith("::")]
        assert annotations, "a starved queue in a run nobody opens is the whole failure"
        assert annotations[0].startswith("::error title=Outbox ledger unwritable::")
        assert "truequote" in annotations[0] and "profitwizard" in annotations[0]

    def test_it_is_logged_at_error_naming_the_starved_products(self, monkeypatch, caplog) -> None:
        with caplog.at_level(logging.ERROR):
            self._run(monkeypatch, self._outcomes())
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("LEDGER UNWRITABLE" in message for message in errors)
        assert any("truequote, profitwizard" in message for message in errors)


class TestScopeOutcomesInTheRunsOwnOutput:
    """The caller workflow runs every feed job for every contractor now, so the
    run's own output is where "this feed did not export, and why" has to appear.

    Two different facts, deliberately worded differently and treated differently:
    a feed the tenant never had is named and nothing else; a feed they HAD and
    lost turns the run red.
    """

    def _run(self, monkeypatch, summary):  # type: ignore[no-untyped-def]
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch(
                "st_exporter.cli.TradeRatedSettings",
                return_value=MagicMock(configured=False, images_configured=False),
            ),
            patch("st_exporter.cli.run_export", return_value=summary),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        return exc_info.value.code

    def test_a_never_granted_tab_is_named_and_the_run_stays_green(
        self, monkeypatch, capsys
    ) -> None:
        """An absent tab is how the contract says "not bought", and an absence is
        not something a reader notices. So it is stated — but it is not a
        failure: it is the ordinary state of a tab belonging to an entity this
        contractor's ServiceTitan app does not cover. `pricebook.materials` on a
        TrueQuote-only tenant is exactly that, on every run, forever."""
        code = self._run(
            monkeypatch,
            _summary(scope_not_granted={"pricebook.materials": "Pricebook -> Materials"}),
        )
        assert code == 0
        assert "not_granted=pricebook_materials" in capsys.readouterr().out

    def test_a_revoked_tab_turns_the_run_red(self, monkeypatch, capsys) -> None:
        """This one worked before. A tab that stops exporting must never end
        green — that silence is the whole failure mode the per-feed repository
        variables were deleted for."""
        code = self._run(monkeypatch, _summary(scope_revoked={"jobs": "HTTP 403: nope"}))
        assert code != 0
        assert "scope_revoked=jobs" in capsys.readouterr().out

    def test_the_two_are_never_confused_in_the_line(self, monkeypatch, capsys) -> None:
        code = self._run(
            monkeypatch,
            _summary(
                scope_not_granted={"reporting.jobCosts": "Reporting"},
                scope_revoked={"jobs": "403"},
            ),
        )
        assert code != 0
        out = capsys.readouterr().out
        assert "not_granted=reporting_jobCosts" in out
        assert "scope_revoked=jobs" in out

    def test_an_ordinary_run_says_neither(self, monkeypatch, capsys) -> None:
        """The markers must not cry wolf: a run where every requested feed
        exported carries no scope words at all."""
        assert self._run(monkeypatch, _summary()) == 0
        out = capsys.readouterr().out
        assert "not_granted" not in out and "scope_revoked" not in out

    def test_a_drain_only_run_has_no_export_summary_to_read(self, monkeypatch) -> None:
        """`--feeds outbox` never calls `run_export`, so there is no summary and
        nothing for the scope check to dereference. The drain is not a feed and
        is not scope-gated — see `run.EXPORT_FEEDS`."""
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "outbox"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export") as mock_run,
            patch("st_exporter.cli._drain_outboxes", return_value=[]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0
        mock_run.assert_not_called()


class TestAFailedFeedRedsTheRun:
    """A top-level feed that was asked for and did not export must not end green.

    Ticket 21 put `jobs` and `technicians` behind ``_guarded_feed`` so that a
    dying feed could no longer throw past the `_meta` write and discard another
    feed's cursor. Catching the exception was right; what shipped with it was
    not — the run then exited 0, and the ONLY trace of a feed that exported
    nothing was a `feed_failed=` token in the summary line. A reader who does not
    know that token sees `jobs=0` and a green tick, which is precisely the
    silence the per-feed repository variables were deleted to eliminate.

    Exiting non-zero costs nothing now: `_meta` is written inside `_run`, before
    the CLI decides the exit code, and the drain has already finished. No cursor
    lost, no tab lost, no outbox item redelivered — the same reasons the original
    crash was tolerable, minus the crash.

    The other direction is a regression too, and is held below: a pricebook or
    financial PER-TAB failure was green before this change and stays green.
    """

    def _run(self, monkeypatch, summary, feeds="jobs,technicians"):  # type: ignore[no-untyped-def]
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", feeds])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch(
                "st_exporter.cli.TradeRatedSettings",
                return_value=MagicMock(configured=False, images_configured=False),
            ),
            patch("st_exporter.cli.run_export", return_value=summary),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        return exc_info.value.code

    def test_a_failed_jobs_feed_exits_one(self, monkeypatch, capsys) -> None:
        """The reproduction from the review, at the CLI: a non-403 on the jobs
        feed gave `EXIT: 0`, `jobs=0 technicians=1 ... feed_failed=jobs`."""
        code = self._run(
            monkeypatch,
            _summary(
                jobs_row_count=0,
                technicians_row_count=1,
                feed_failures={"jobs": "HTTP 500: upstream is down"},
            ),
        )
        assert code == 1
        assert "feed_failed=jobs" in capsys.readouterr().out

    def test_a_failed_technicians_feed_exits_one_too(self, monkeypatch, capsys) -> None:
        """Both feeds `_guarded_feed` covers, not only the one in the report."""
        code = self._run(
            monkeypatch,
            _summary(feed_failures={"technicians": "HTTP 400: Unknown parameter 'active'"}),
        )
        assert code == 1
        assert "feed_failed=technicians" in capsys.readouterr().out

    def test_a_clean_run_is_still_green(self, monkeypatch) -> None:
        """The marker must not cry wolf: no `feed_failures`, no red run."""
        assert self._run(monkeypatch, _summary()) == 0

    def test_a_failed_pricebook_tab_still_exits_zero(self, monkeypatch, capsys) -> None:
        """The regression in the OTHER direction, guarded explicitly.

        One tab of the four-tab catalogue feed left at last run's contents is a
        warning and was green on 0.2.9. Reddening it here would be a new
        regression of exactly the kind this change is fixing, in reverse — and
        `pricebook.materials` 403ing on a TrueQuote-only tenant is the ordinary
        every-cycle state, so a red run there would be noise forever.
        """
        code = self._run(
            monkeypatch,
            _summary(
                pricebook_row_counts={"pricebook.services": 2},
                pricebook_failures={"pricebook.equipment": "HTTP 500: boom"},
            ),
            feeds="pricebook",
        )
        assert code == 0
        assert "pricebook_failed=pricebook_equipment" in capsys.readouterr().out

    def test_a_failed_financial_tab_still_exits_zero(self, monkeypatch, capsys) -> None:
        code = self._run(
            monkeypatch,
            _summary(
                financial_row_counts={"accounting.invoices": 5},
                financial_failures={"reporting.jobCosts": "HTTP 429: slow down"},
            ),
            feeds="financial",
        )
        assert code == 0
        assert "financial_failed=reporting.jobCosts" in capsys.readouterr().out

    def test_a_feed_failure_and_a_healthy_pricebook_still_reds_the_run(
        self, monkeypatch, capsys
    ) -> None:
        """Mixed run: only `feed_failures` decides, and it decides on its own."""
        code = self._run(
            monkeypatch,
            _summary(
                feed_failures={"jobs": "HTTP 429: slow down"},
                pricebook_row_counts={"pricebook.services": 2},
            ),
            feeds="jobs,pricebook",
        )
        assert code == 1
        out = capsys.readouterr().out
        assert "feed_failed=jobs" in out and "pricebook_failed" not in out


class TestTheImagesFeedIsRoutedOnItsOwn:
    """`images` is its own feed and its own workflow job.

    It used to be `--upload-images` riding inside a `--feeds pricebook` run. On
    a tenant with ~7,191 assets that run was SIGKILLed by the runner at 10m35s
    with 0 images uploaded (run 35130164187 on `BBTT-01/tr-doorservpro`) and
    took the hourly pricebook export down with it.
    """

    @staticmethod
    @contextmanager
    def _patches(images_configured: bool = True):
        """The three settings loaders, as one context manager.

        A tuple of patches cannot be star-unpacked into a `with`, and every test
        below needs the same three.
        """
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch(
                "st_exporter.cli.TradeRatedSettings",
                return_value=MagicMock(
                    configured=False,
                    images_configured=images_configured,
                    image_upload_token="tqm_image",
                    image_base_url="https://truequote.example.com/api/outbox",
                ),
            ),
        ):
            yield

    def test_feeds_images_runs_the_image_pass_and_no_export(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "images"])
        image_summary = ImageUploadSummary(uploaded=7, pending=12, stopped="budget")
        with (
            self._patches(),
            patch("st_exporter.cli.run_export") as mock_export,
            patch("st_exporter.cli.run_images", return_value=image_summary) as mock_images,
            patch("st_exporter.cli.TrueQuoteImageClient") as mock_client,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        # No export half at all: it writes no tab and no `_meta` row, which is
        # what earns its job a concurrency lock of its own.
        mock_export.assert_not_called()
        mock_images.assert_called_once()
        assert mock_images.call_args.kwargs["image_client"] is mock_client.return_value
        out = capsys.readouterr().out
        assert "images_uploaded=7" in out
        # `pending` and `stopped` are what tell an operator a sweep is
        # converging rather than failing.
        assert "images_pending=12" in out
        assert "images_stopped=budget" in out
        # A refusal TrueQuote made this run, and one it made on an earlier run
        # that this run therefore did not re-send. Both on the output line: a
        # `rejected=1` nobody can see is the defect this pair closes.
        assert "images_rejected=0" in out
        assert "images_rejected_remembered=0" in out

    def test_feeds_pricebook_no_longer_uploads_any_bytes(self, monkeypatch) -> None:
        """THE BEHAVIOUR CHANGE, stated.

        A connector that repins and leaves TRUEQUOTE_IMAGE_TOKEN on its
        `pricebook-feed` job uploads nothing from there. That is deliberate and
        it is the SAFE direction: what that job used to do on a large catalogue
        was get killed mid-download every hour, delivering nothing anyway and
        failing the pricebook export as well. Identifiers still reach the Sheet.
        """
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "pricebook"])
        with (
            self._patches(),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli.run_images") as mock_images,
            patch("st_exporter.cli.TrueQuoteImageClient") as mock_client,
            pytest.raises(SystemExit),
        ):
            main()

        mock_images.assert_not_called()
        mock_client.assert_not_called()

    def test_holding_the_token_without_the_feed_is_loud(self, monkeypatch, caplog) -> None:
        """...and it must not be SILENT, or a connector that repins without
        adding the job simply stops delivering images on green runs — the exact
        silent-stop shape the one-drain rule exists to prevent."""
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "pricebook"])
        with (
            self._patches(),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            caplog.at_level(logging.WARNING),
            pytest.raises(SystemExit),
        ):
            main()

        assert "images-feed" in caplog.text
        assert "TRUEQUOTE_IMAGE_TOKEN" in caplog.text

    def test_no_token_means_a_quiet_no_op(self, monkeypatch, caplog) -> None:
        """A contractor who never bought TrueQuote must not read a warning every
        run. A missing token is the EXPECTED state, not an error."""
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "images"])
        with (
            self._patches(images_configured=False),
            patch("st_exporter.cli.run_images") as mock_images,
            caplog.at_level(logging.WARNING),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_images.assert_not_called()
        assert "TRUEQUOTE_IMAGE_TOKEN" not in caplog.text

    def test_no_upload_images_still_switches_it_off(self, monkeypatch) -> None:
        """The flag was kept rather than removed, so a caller already passing
        `--no-upload-images` keeps working. It just points at `images` now."""
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "images", "--no-upload-images"])
        with (
            self._patches(),
            patch("st_exporter.cli.run_images") as mock_images,
            pytest.raises(SystemExit),
        ):
            main()

        mock_images.assert_not_called()

    def test_a_dry_run_uploads_nothing(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "images", "--dry-run"])
        with (
            self._patches(),
            patch("st_exporter.cli.run_images") as mock_images,
            pytest.raises(SystemExit),
        ):
            main()

        mock_images.assert_not_called()


class TestTheCapOnTheRunsOwnOutputLine:
    """A connector runs a bounded proving pass by passing `image_max_assets`.
    The run's output has to say what the cap was and whether it bit, or the
    operator cannot tell a cap stop from a clock stop."""

    def _line(self, images: ImageUploadSummary) -> str:
        from st_exporter.cli import _image_fields

        return _image_fields(images)

    def test_a_capped_run_names_the_cap_and_the_hit(self) -> None:
        from st_exporter.images.upload import ASSET_CAP_REACHED

        line = self._line(
            ImageUploadSummary(uploaded=500, fetched=500, max_assets=500, stopped=ASSET_CAP_REACHED)
        )
        assert "images_max_assets=500" in line
        assert "images_cap_hit=true" in line
        assert f"images_stopped={ASSET_CAP_REACHED}" in line

    def test_an_uncapped_run_says_none_not_zero(self) -> None:
        line = self._line(ImageUploadSummary(uploaded=3))
        assert "images_max_assets=none" in line
        assert "images_cap_hit=false" in line

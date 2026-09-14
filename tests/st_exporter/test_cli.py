"""Tests for the `st-export` CLI entrypoint: flag wiring and error handling.

``main()`` wraps ``typer.run(run_once)``, which parses ``sys.argv`` directly and
always raises ``SystemExit`` (click's standalone mode) — even on success — so
every test patches ``sys.argv`` and calls ``main()`` directly rather than going
through Typer's ``CliRunner`` (which expects a Click/Typer app object, not a
plain wrapper function).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import httpx
import pydantic
import pytest

from st_cli.exceptions import ConfigError
from st_exporter.cli import main
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
            image_client=None,
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
        mock_run.assert_called_once_with(
            "s", "e", feeds=DEFAULT_FEEDS, dry_run=True, image_client=None
        )

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
        mock_run.assert_called_once_with(
            "s", "e", feeds=frozenset({"pricebook"}), dry_run=False, image_client=None
        )


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
        mock_run.assert_called_once_with(
            "s", "e", feeds=frozenset({"jobs"}), dry_run=False, image_client=None
        )

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

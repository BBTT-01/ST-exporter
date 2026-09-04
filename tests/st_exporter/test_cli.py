"""Tests for the `st-export` CLI entrypoint: flag wiring and error handling.

``main()`` wraps ``typer.run(run_once)``, which parses ``sys.argv`` directly and
always raises ``SystemExit`` (click's standalone mode) — even on success — so
every test patches ``sys.argv`` and calls ``main()`` directly rather than going
through Typer's ``CliRunner`` (which expects a Click/Typer app object, not a
plain wrapper function).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pydantic
import pytest

from st_cli.exceptions import ConfigError
from st_exporter.cli import main
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
            patch("st_exporter.cli.TradeRatedSettings", return_value=MagicMock(configured=False)),
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
            pricebook=False,
            dry_run=False,
        )

    def test_dry_run_flag_is_passed_through(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--dry-run"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.TradeRatedSettings", return_value=MagicMock(configured=False)),
            patch("st_exporter.cli.run_export", return_value=_summary(dry_run=True)) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_run.assert_called_once_with(
            "s", "e", feeds=DEFAULT_FEEDS, pricebook=False, dry_run=True
        )

    def test_pricebook_flag_is_passed_through(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--pricebook"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.TradeRatedSettings", return_value=MagicMock(configured=False)),
            patch("st_exporter.cli.run_export", return_value=_summary()) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_run.assert_called_once_with(
            "s", "e", feeds=DEFAULT_FEEDS, pricebook=True, dry_run=False
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
            "s", "e", feeds=frozenset({"jobs"}), pricebook=False, dry_run=False
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
    def test_drains_outbox_when_configured_and_not_dry_run(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0])
        fake_traderated_settings = MagicMock(configured=True)
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.TradeRatedSettings", return_value=fake_traderated_settings),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli._drain_outbox") as mock_drain,
            pytest.raises(SystemExit),
        ):
            main()
        mock_drain.assert_called_once_with("s", "e", fake_traderated_settings)

    def test_skips_outbox_drain_when_not_configured(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0])
        fake_traderated_settings = MagicMock(configured=False)
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.TradeRatedSettings", return_value=fake_traderated_settings),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli._drain_outbox") as mock_drain,
            pytest.raises(SystemExit),
        ):
            main()
        mock_drain.assert_not_called()

    def test_skips_outbox_drain_on_dry_run_even_if_configured(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--dry-run"])
        fake_traderated_settings = MagicMock(configured=True)
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.TradeRatedSettings", return_value=fake_traderated_settings),
            patch("st_exporter.cli.run_export", return_value=_summary(dry_run=True)),
            patch("st_exporter.cli._drain_outbox") as mock_drain,
            pytest.raises(SystemExit),
        ):
            main()
        mock_drain.assert_not_called()

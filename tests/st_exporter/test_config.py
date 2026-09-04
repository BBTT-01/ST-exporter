"""Tests for ExporterSettings — env-only loading and validation."""

from __future__ import annotations

import pydantic
import pytest

from st_exporter.config import ExporterSettings

_REQUIRED_ENV = {
    "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
    "GOOGLE_SHEET_ID": "sheet-1",
    "GOOGLE_RAW_CACHE_SHEET_ID": "raw-1",
}


def _set_env(monkeypatch, **overrides: str) -> None:
    env = {**_REQUIRED_ENV, **overrides}
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_loads_from_env_with_default_window_days(monkeypatch) -> None:
    _set_env(monkeypatch)
    settings = ExporterSettings()  # type: ignore[call-arg]
    assert settings.sheet_id == "sheet-1"
    assert settings.raw_cache_sheet_id == "raw-1"
    assert settings.window_days == 90


def test_missing_required_field_raises_validation_error(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-1")
    monkeypatch.delenv("GOOGLE_RAW_CACHE_SHEET_ID", raising=False)
    with pytest.raises(pydantic.ValidationError):
        ExporterSettings()  # type: ignore[call-arg]


def test_window_days_alias_overrides_google_prefixed_default(monkeypatch) -> None:
    _set_env(monkeypatch, EXPORTER_WINDOW_DAYS="30")
    settings = ExporterSettings()  # type: ignore[call-arg]
    assert settings.window_days == 30


def test_window_days_zero_is_rejected(monkeypatch) -> None:
    _set_env(monkeypatch, EXPORTER_WINDOW_DAYS="0")
    with pytest.raises(pydantic.ValidationError):
        ExporterSettings()  # type: ignore[call-arg]


def test_window_days_negative_is_rejected(monkeypatch) -> None:
    _set_env(monkeypatch, EXPORTER_WINDOW_DAYS="-90")
    with pytest.raises(pydantic.ValidationError):
        ExporterSettings()  # type: ignore[call-arg]


def test_window_days_non_integer_is_rejected(monkeypatch) -> None:
    _set_env(monkeypatch, EXPORTER_WINDOW_DAYS="abc")
    with pytest.raises(pydantic.ValidationError):
        ExporterSettings()  # type: ignore[call-arg]


def test_service_account_json_is_excluded_from_repr(monkeypatch) -> None:
    _set_env(monkeypatch, GOOGLE_SERVICE_ACCOUNT_JSON='{"secret": "sentinel-value"}')
    settings = ExporterSettings()  # type: ignore[call-arg]
    assert "sentinel-value" not in repr(settings)

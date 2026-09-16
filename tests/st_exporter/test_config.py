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


def test_pricebook_category_ids_default_to_the_whole_catalogue(monkeypatch) -> None:
    _set_env(monkeypatch)
    assert ExporterSettings().pricebook_category_ids == ()  # type: ignore[call-arg]


def test_pricebook_category_ids_split_on_commas(monkeypatch) -> None:
    _set_env(monkeypatch, EXPORTER_PRICEBOOK_CATEGORY_IDS=" 10, 11 ,")
    assert ExporterSettings().pricebook_category_ids == ("10", "11")  # type: ignore[call-arg]


def test_financial_window_defaults_to_ninety_but_is_a_separate_knob(monkeypatch) -> None:
    # Same number as the jobs window, different reason — and raising one must
    # never move the other. See window.FINANCIAL_WINDOW_DAYS.
    _set_env(monkeypatch, EXPORTER_FINANCIAL_WINDOW_DAYS="365")
    settings = ExporterSettings()  # type: ignore[call-arg]
    assert settings.financial_window_days == 365
    assert settings.window_days == 90


def test_financial_window_zero_is_rejected(monkeypatch) -> None:
    # Zero would silently empty three money tabs rather than error.
    _set_env(monkeypatch, EXPORTER_FINANCIAL_WINDOW_DAYS="0")
    with pytest.raises(pydantic.ValidationError):
        ExporterSettings()  # type: ignore[call-arg]


def test_financial_max_jobs_defaults_and_rejects_zero(monkeypatch) -> None:
    _set_env(monkeypatch)
    assert ExporterSettings().financial_max_jobs == 500  # type: ignore[call-arg]
    _set_env(monkeypatch, EXPORTER_FINANCIAL_MAX_JOBS="0")
    with pytest.raises(pydantic.ValidationError):
        ExporterSettings()  # type: ignore[call-arg]


def test_the_job_timeout_defaults_to_the_workflows_own_default(monkeypatch) -> None:
    # The two numbers are the same number. If the workflow's default moves and
    # this one does not, a run with no env var set budgets against a clock that
    # is not the one that will kill it.
    _set_env(monkeypatch)
    assert ExporterSettings().job_timeout_minutes == 10  # type: ignore[call-arg]


def test_the_image_budget_stops_short_of_the_job_timeout(monkeypatch) -> None:
    # The reserve covers checkout, setup-python and `pip install` — all of which
    # spend the job's clock before this process exists — plus the ledger flush.
    _set_env(monkeypatch, EXPORTER_JOB_TIMEOUT_MINUTES="25")
    assert ExporterSettings().image_budget_seconds == 23 * 60  # type: ignore[call-arg]


def test_a_tiny_job_timeout_still_buys_a_minute_of_uploading(monkeypatch) -> None:
    # A budget of zero is not "run briefly", it is "never upload anything".
    _set_env(monkeypatch, EXPORTER_JOB_TIMEOUT_MINUTES="1")
    assert ExporterSettings().image_budget_seconds == 60  # type: ignore[call-arg]


def test_a_job_timeout_of_zero_is_rejected(monkeypatch) -> None:
    _set_env(monkeypatch, EXPORTER_JOB_TIMEOUT_MINUTES="0")
    with pytest.raises(pydantic.ValidationError):
        ExporterSettings()  # type: ignore[call-arg]

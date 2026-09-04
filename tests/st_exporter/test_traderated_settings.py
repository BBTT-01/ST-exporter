"""Tests for TradeRatedSettings — optional-by-design outbox configuration."""

from __future__ import annotations

from st_exporter.traderated_settings import TradeRatedSettings


class TestTradeRatedSettings:
    def test_unset_is_not_configured(self, monkeypatch) -> None:
        monkeypatch.delenv("TRADERATED_MACHINE_TOKEN", raising=False)
        monkeypatch.delenv("TRADERATED_OUTBOX_BASE_URL", raising=False)
        settings = TradeRatedSettings()
        assert settings.configured is False

    def test_both_set_is_configured(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "tok")
        monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "https://example.com")
        settings = TradeRatedSettings()
        assert settings.configured is True

    def test_only_one_set_is_not_configured(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "tok")
        monkeypatch.delenv("TRADERATED_OUTBOX_BASE_URL", raising=False)
        settings = TradeRatedSettings()
        assert settings.configured is False

    def test_empty_string_secrets_are_not_configured(self, monkeypatch) -> None:
        """GitHub Actions maps an ``env:`` entry for an unset secret to the empty
        string, not to nothing — so pydantic loads ``""`` (non-None) for both.
        An ``is not None`` check would call that configured and the drain would
        then build an httpx client on an empty base URL and crash the run."""
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "")
        monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "")
        settings = TradeRatedSettings()
        assert settings.configured is False

    def test_empty_string_for_only_one_secret_is_not_configured(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "tok")
        monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "")
        settings = TradeRatedSettings()
        assert settings.configured is False

    def test_machine_token_not_in_repr(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "super-secret-token")
        settings = TradeRatedSettings()
        assert "super-secret-token" not in repr(settings)

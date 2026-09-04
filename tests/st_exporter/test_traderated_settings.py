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

    def test_machine_token_not_in_repr(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "super-secret-token")
        settings = TradeRatedSettings()
        assert "super-secret-token" not in repr(settings)

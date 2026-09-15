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


class TestImageUploadLane:
    """The image lane has its OWN token: TrueQuote mints machine tokens per
    scope and 401s a token presented to the wrong one, so a configured booking
    outbox says nothing about whether images can be uploaded."""

    def test_not_configured_without_the_image_token(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "booking-tok")
        monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "https://tq.example.com/api/outbox")
        settings = TradeRatedSettings()
        assert settings.configured is True
        assert settings.images_configured is False

    def test_configured_with_both_values(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_IMAGE_TOKEN", "image-tok")
        monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "https://tq.example.com/api/outbox")
        settings = TradeRatedSettings()
        assert settings.images_configured is True
        # The booking lane is independently unconfigured.
        assert settings.configured is False

    def test_empty_string_image_token_is_not_configured(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_IMAGE_TOKEN", "")
        monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "https://tq.example.com/api/outbox")
        assert TradeRatedSettings().images_configured is False

    def test_image_token_not_in_repr(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_IMAGE_TOKEN", "super-secret-image-token")
        assert "super-secret-image-token" not in repr(TradeRatedSettings())

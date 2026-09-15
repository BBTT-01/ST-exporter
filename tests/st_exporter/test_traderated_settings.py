"""Tests for TradeRatedSettings — optional-by-design outbox configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from st_exporter.traderated_settings import TradeRatedSettings
from st_exporter.url_validation import InvalidOutboxUrlError


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


class TestUrlValidation:
    """Both URL fields are guarded at load, because ``image_base_url`` hands
    whichever is set straight to a client that sends the Machine Token."""

    def test_https_is_accepted(self, monkeypatch) -> None:
        monkeypatch.setenv("TRUEQUOTE_OUTBOX_URL", "https://tq.example.com/api/outbox")
        assert TradeRatedSettings().image_base_url == "https://tq.example.com/api/outbox"

    @pytest.mark.parametrize("env_var", ["TRADERATED_OUTBOX_BASE_URL", "TRUEQUOTE_OUTBOX_URL"])
    def test_http_is_refused_under_either_spelling(self, monkeypatch, env_var: str) -> None:
        monkeypatch.setenv(env_var, "http://tq.example.com/api/outbox")
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            TradeRatedSettings()
        assert env_var in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_url",
        [
            "tq.example.com/api/outbox",
            "https://",
            "https://user:pass@tq.example.com",
            "https://tq.example.com?token=leak",
            "https://tq.example.com#frag",
        ],
    )
    def test_malformed_urls_are_refused(self, monkeypatch, bad_url: str) -> None:
        monkeypatch.setenv("TRUEQUOTE_OUTBOX_URL", bad_url)
        with pytest.raises(InvalidOutboxUrlError):
            TradeRatedSettings()

    def test_the_error_is_not_wrapped_in_a_pydantic_validationerror(self, monkeypatch) -> None:
        """A ``ValidationError`` would bury the actionable sentence in pydantic's
        own formatting; ``InvalidOutboxUrlError`` is not a ``ValueError``, so it
        reaches ``cli.main``'s ``Error: ...`` path intact."""
        monkeypatch.setenv("TRUEQUOTE_OUTBOX_URL", "http://tq.example.com")
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            TradeRatedSettings()
        assert not isinstance(excinfo.value, ValidationError)

    def test_absent_secrets_still_load_cleanly(self, monkeypatch) -> None:
        """An unbought product: nothing set at all must not become a hard failure."""
        settings = TradeRatedSettings()
        assert settings.configured is False
        assert settings.images_configured is False
        assert settings.image_base_url is None

    def test_empty_string_secrets_still_load_cleanly(self, monkeypatch) -> None:
        """What GitHub Actions actually passes for an unset secret."""
        monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "")
        monkeypatch.setenv("TRUEQUOTE_OUTBOX_URL", "")
        settings = TradeRatedSettings()
        assert settings.configured is False
        assert settings.images_configured is False

    def test_the_traderated_fallback_still_works(self, monkeypatch) -> None:
        """The image lane prefers ``TRUEQUOTE_*`` and falls back to the older
        ``TRADERATED_*`` names — validation must not break either path."""
        monkeypatch.setenv("TRADERATED_IMAGE_TOKEN", "image-tok")
        monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "https://old.example.com/api/outbox")
        assert TradeRatedSettings().image_base_url == "https://old.example.com/api/outbox"

        monkeypatch.setenv("TRUEQUOTE_OUTBOX_URL", "https://new.example.com/api/outbox")
        monkeypatch.setenv("TRUEQUOTE_IMAGE_TOKEN", "tq-image-tok")
        settings = TradeRatedSettings()
        assert settings.image_base_url == "https://new.example.com/api/outbox"
        assert settings.images_configured is True

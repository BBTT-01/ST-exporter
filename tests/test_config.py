"""Tests for config.py."""

import pytest

from st_cli.config import Environment, Settings


@pytest.fixture(autouse=True)
def clear_env_vars(monkeypatch):
    """Prevent .env file from leaking into tests."""
    monkeypatch.delenv("ST_ENVIRONMENT", raising=False)
    monkeypatch.delenv("ST_CLIENT_ID", raising=False)
    monkeypatch.delenv("ST_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("ST_APP_KEY", raising=False)
    monkeypatch.delenv("ST_TENANT_ID", raising=False)


class TestEnvironment:
    """Exact equality, not ``in``.

    ``"auth.servicetitan.io" in env.auth_url`` passes for
    ``https://evil.example/auth.servicetitan.io`` — the substring-on-a-URL
    pattern CodeQL flags as ``py/incomplete-url-substring-sanitization``. Here
    the URLs are constants the code itself defines, so there was no
    vulnerability to exploit, but the assertion was also weaker than it looked:
    a typo'd host that still contained the substring would have passed. Pinning
    the whole string removes the flagged pattern and catches the typo.
    """

    def test_production_urls(self):
        env = Environment.PRODUCTION
        assert env.auth_url == "https://auth.servicetitan.io/connect/token"
        assert env.api_base == "https://api.servicetitan.io"

    def test_integration_urls(self):
        env = Environment.INTEGRATION
        assert env.auth_url == "https://auth-integration.servicetitan.io/connect/token"
        assert env.api_base == "https://api-integration.servicetitan.io"

    def test_the_two_environments_share_no_urls(self):
        """The reason the `integration`/`not integration` assertions existed:
        production must never be served the integration host, or vice versa."""
        assert Environment.PRODUCTION.auth_url != Environment.INTEGRATION.auth_url
        assert Environment.PRODUCTION.api_base != Environment.INTEGRATION.api_base


class TestSettings:
    def test_creates_with_required_fields(self):
        s = Settings(
            client_id="cid",
            client_secret="csec",
            app_key="key",
            tenant_id=1,
            _env_file=None,
        )
        assert s.client_id == "cid"
        assert s.tenant_id == 1
        assert s.environment == Environment.PRODUCTION

    def test_defaults_to_production(self):
        s = Settings(
            client_id="a",
            client_secret="b",
            app_key="c",
            tenant_id=1,
            _env_file=None,
        )
        assert s.environment == Environment.PRODUCTION

    def test_auth_url_delegates_to_environment(self):
        s = Settings(
            client_id="a",
            client_secret="b",
            app_key="c",
            tenant_id=1,
            environment=Environment.INTEGRATION,
            _env_file=None,
        )
        assert s.auth_url == Environment.INTEGRATION.auth_url

    def test_api_base_delegates_to_environment(self):
        s = Settings(
            client_id="a",
            client_secret="b",
            app_key="c",
            tenant_id=1,
            environment=Environment.INTEGRATION,
            _env_file=None,
        )
        assert s.api_base == Environment.INTEGRATION.api_base

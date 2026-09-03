"""Shared fixtures for st_exporter tests."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from st_cli.config import Environment, Settings
from st_exporter.config import ExporterSettings


@pytest.fixture()
def st_settings() -> Settings:
    return Settings(
        client_id="test-id",
        client_secret="test-secret",
        app_key="test-key",
        tenant_id=12345,
        environment=Environment.PRODUCTION,
    )


@pytest.fixture()
def exporter_settings() -> ExporterSettings:
    return ExporterSettings(
        service_account_json="{}",
        sheet_id="export-store-sheet-id",
        raw_cache_sheet_id="raw-cache-sheet-id",
    )


def mock_auth_token(auth_url: str, token: str = "test-token") -> None:
    """Register a respx route for the OAuth token endpoint.

    Must be called from inside an active ``@respx.mock``-decorated test — an
    autouse fixture can't do this itself, since fixture setup runs before the
    decorator activates respx's mock context.
    """
    respx.post(auth_url).mock(
        return_value=httpx.Response(200, json={"access_token": token, "expires_in": 3600})
    )


@pytest.fixture(autouse=True)
def clean_token_cache(tmp_path):
    """Redirect st_cli's on-disk OAuth token cache to tmp_path for test isolation
    — mirrors the existing tests/test_auth.py convention. Without this, every test
    that builds a real ServiceTitanClient would read/write the developer's actual
    ``~/.st_cli/token_cache.json``.
    """
    cache_dir = tmp_path / ".st_cli"
    cache_file = cache_dir / "token_cache.json"
    with patch("st_cli.auth._CACHE_DIR", cache_dir), patch("st_cli.auth._CACHE_FILE", cache_file):
        yield

"""Tests for auth.py."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest
import respx

from st_cli.auth import TokenManager
from st_cli.config import Environment, Settings
from st_cli.exceptions import AuthError


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        client_id="test-id",
        client_secret="test-secret",
        app_key="test-key",
        tenant_id=12345,
        environment=Environment.PRODUCTION,
    )


@pytest.fixture(autouse=True)
def clean_cache(tmp_path):
    """Redirect cache to tmp_path for isolation."""
    cache_dir = tmp_path / ".st_cli"
    cache_file = cache_dir / "token_cache.json"
    with patch("st_cli.auth._CACHE_DIR", cache_dir), patch("st_cli.auth._CACHE_FILE", cache_file):
        yield cache_dir, cache_file


class TestTokenManager:
    @respx.mock
    def test_fetches_new_token(self, settings, clean_cache):
        route = respx.post(settings.auth_url).mock(
            return_value=httpx.Response(200, json={"access_token": "tok123", "expires_in": 3600})
        )
        tm = TokenManager(settings)
        token = tm.get_token()
        assert token == "tok123"
        assert route.called

    @respx.mock
    def test_caches_in_memory(self, settings, clean_cache):
        route = respx.post(settings.auth_url).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        tm = TokenManager(settings)
        tm.get_token()
        tm.get_token()
        assert route.call_count == 1

    @respx.mock
    def test_force_refresh_fetches_new_token(self, settings, clean_cache):
        route = respx.post(settings.auth_url).mock(
            return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        )
        tm = TokenManager(settings)
        tm.get_token()
        token = tm.force_refresh()
        assert token == "new"
        assert route.call_count == 2

    @respx.mock
    def test_saves_to_file_cache(self, settings, clean_cache):
        _, cache_file = clean_cache
        respx.post(settings.auth_url).mock(
            return_value=httpx.Response(200, json={"access_token": "file-tok", "expires_in": 3600})
        )
        tm = TokenManager(settings)
        tm.get_token()
        assert cache_file.exists()
        raw = json.loads(cache_file.read_text())
        key = f"{settings.client_id}:{settings.tenant_id}"
        assert raw[key]["token"] == "file-tok"

    @respx.mock
    def test_loads_from_file_cache(self, settings, clean_cache):
        cache_dir, cache_file = clean_cache
        key = f"{settings.client_id}:{settings.tenant_id}"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({key: {"token": "cached", "expires_at": time.time() + 3600}})
        )
        route = respx.post(settings.auth_url)
        tm = TokenManager(settings)
        token = tm.get_token()
        assert token == "cached"
        assert not route.called

    @respx.mock
    def test_expired_file_cache_triggers_refresh(self, settings, clean_cache):
        cache_dir, cache_file = clean_cache
        key = f"{settings.client_id}:{settings.tenant_id}"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({key: {"token": "old", "expires_at": time.time() - 100}}))
        respx.post(settings.auth_url).mock(
            return_value=httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        )
        tm = TokenManager(settings)
        token = tm.get_token()
        assert token == "fresh"

    @respx.mock
    def test_auth_error_on_http_failure(self, settings, clean_cache):
        respx.post(settings.auth_url).mock(return_value=httpx.Response(401, text="bad creds"))
        tm = TokenManager(settings)
        with pytest.raises(AuthError):
            tm.get_token()


class TestTheTokenManagerIsSafeToShare:
    """The image pass runs a pool of workers against ONE client.

    Without a lock, every thread discovers the same expired token at the same
    moment: N simultaneous POSTs to the auth endpoint (itself rate limited) and
    N interleaved read-modify-writes of one JSON cache file, which is how that
    file ends up truncated.
    """

    @respx.mock
    def test_concurrent_first_use_issues_exactly_one_token_request(
        self, settings, clean_cache
    ) -> None:
        barrier = threading.Barrier(8)

        def slow(request: httpx.Request) -> httpx.Response:
            time.sleep(0.02)
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})

        route = respx.post(settings.auth_url).mock(side_effect=slow)
        manager = TokenManager(settings)

        def get(_: int) -> str:
            barrier.wait()
            return manager.get_token()

        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(get, range(8)))

        assert tokens == ["tok"] * 8
        assert route.call_count == 1, (
            f"{route.call_count} threads each refreshed the same token — a stampede "
            "against a rate-limited endpoint, and eight writers to one cache file"
        )

    @respx.mock
    def test_force_refresh_does_not_stampede_when_many_threads_see_one_401(
        self, settings, clean_cache
    ) -> None:
        """ "Force" means "do not trust the token I just used", not "issue a
        request whatever else is happening". A token another thread has already
        replaced IS the answer — asking for a second one is the stampede."""
        issued = iter(f"tok-{n}" for n in range(1, 50))
        barrier = threading.Barrier(8)

        def slow(request: httpx.Request) -> httpx.Response:
            token = next(issued)
            time.sleep(0.02)
            return httpx.Response(200, json={"access_token": token, "expires_in": 900})

        route = respx.post(settings.auth_url).mock(side_effect=slow)
        manager = TokenManager(settings)
        stale = manager.get_token()
        assert route.call_count == 1

        def refresh(_: int) -> str:
            barrier.wait()
            return manager.force_refresh()

        with ThreadPoolExecutor(max_workers=8) as pool:
            after = set(pool.map(refresh, range(8)))

        assert route.call_count == 2, (
            f"eight threads sharing one 401 issued {route.call_count - 1} refreshes"
        )
        assert after == {"tok-2"} and stale not in after

    @respx.mock
    def test_the_cache_file_is_still_valid_json_after_a_concurrent_refresh(
        self, settings, clean_cache
    ) -> None:
        """The failure this lock actually prevents: `_save_to_file` reads the
        whole JSON, mutates it and writes it back."""
        _, cache_file = clean_cache
        respx.post(settings.auth_url).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 900})
        )
        manager = TokenManager(settings)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: manager.force_refresh(), range(16)))

        assert json.loads(cache_file.read_text())[f"{settings.client_id}:{settings.tenant_id}"]

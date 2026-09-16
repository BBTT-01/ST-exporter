"""OAuth 2.0 Client Credentials auth with two-tier caching."""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock

import httpx

from st_cli.config import Settings
from st_cli.exceptions import AuthError

_CACHE_DIR = Path.home() / ".st_cli"
_CACHE_FILE = _CACHE_DIR / "token_cache.json"
_EARLY_EXPIRY_BUFFER = 60  # seconds


class TokenManager:
    """Manages OAuth tokens with in-memory + file-based caching."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token: str | None = None
        self._expires_at: float = 0.0
        # Refreshes are serialised. Without this, a concurrent caller (the image
        # pass runs a pool of workers) can have every thread discover the same
        # expired token at the same moment: N simultaneous POSTs to the auth
        # endpoint — itself rate limited — and N interleaved read-modify-writes
        # of one JSON cache file, which is how that file ends up truncated.
        self._lock = Lock()
        self._load_from_file()

    def get_token(self) -> str:
        """Return a valid token, refreshing if needed."""
        if self._is_valid():
            assert self._token is not None
            return self._token
        with self._lock:
            # Re-check inside the lock: while this thread was waiting, another
            # may already have done the refresh it was waiting for.
            if self._is_valid():
                assert self._token is not None
                return self._token
            return self._refresh()

    def force_refresh(self) -> str:
        """Force a token refresh (used on 401 retry).

        "Force" means "do not trust the token I just used", not "issue a request
        whatever else is happening". If another thread replaced the token while
        this one was waiting for the lock, that new token IS the answer — asking
        for a second one would be the thundering herd this lock exists to stop.
        """
        stale = self._token
        with self._lock:
            if self._token is not None and self._token != stale and self._is_valid():
                return self._token
            return self._refresh()

    def _is_valid(self) -> bool:
        return self._token is not None and time.time() < (self._expires_at - _EARLY_EXPIRY_BUFFER)

    def _refresh(self) -> str:
        try:
            resp = httpx.post(
                self._settings.auth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AuthError(
                f"Token request failed ({exc.response.status_code}): {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise AuthError(f"Token request failed: {exc}") from exc

        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + data["expires_in"]
        self._save_to_file()
        return self._token  # type: ignore[return-value]

    def _load_from_file(self) -> None:
        if not _CACHE_FILE.exists():
            return
        try:
            raw = json.loads(_CACHE_FILE.read_text())
            cache_key = self._cache_key()
            entry = raw.get(cache_key)
            if entry and time.time() < (entry["expires_at"] - _EARLY_EXPIRY_BUFFER):
                self._token = entry["token"]
                self._expires_at = entry["expires_at"]
        except (json.JSONDecodeError, KeyError):
            pass

    def _save_to_file(self) -> None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        raw: dict = {}
        if _CACHE_FILE.exists():
            try:
                raw = json.loads(_CACHE_FILE.read_text())
            except json.JSONDecodeError:
                pass
        raw[self._cache_key()] = {
            "token": self._token,
            "expires_at": self._expires_at,
        }
        _CACHE_FILE.write_text(json.dumps(raw))

    def _cache_key(self) -> str:
        return f"{self._settings.client_id}:{self._settings.tenant_id}"

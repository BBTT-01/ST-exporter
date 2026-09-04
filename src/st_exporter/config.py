"""Exporter settings, loaded from environment variables only.

Deliberately does not offer an ``env_file`` fallback the way ``st_cli.config.Settings``
does for local CLI use — a GitHub Actions runner has no ``.env``, and ticket 05
requires credentials from environment only, so there must be no code path (now or
added later) that reads one.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExporterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOOGLE_")

    # The full service-account JSON as one env var value — never a file path.
    # repr=False so a stray print()/log of the settings object can't leak it.
    service_account_json: str = Field(repr=False)

    # Export Store: the customer-facing Sheet, shared read-only with TradeRated.
    sheet_id: str

    # Private cache Sheet: owned by the exporter's service account, never shared.
    # Holds _raw_customers/_raw_locations/etc. — see the plan's "raw-cache
    # placement" decision for why this is a second spreadsheet, not more tabs on
    # the shared one.
    raw_cache_sheet_id: str

    # Not GOOGLE_-prefixed; bypasses the class prefix via validation_alias.
    # ge=1: a zero or negative window would silently empty the jobs tab rather
    # than error — the exact silent-failure mode this package treats as its
    # highest-stakes risk (see window.py's module docstring).
    window_days: int = Field(default=90, ge=1, validation_alias="EXPORTER_WINDOW_DAYS")

"""Exporter settings, loaded from environment variables only.

Deliberately does not offer an ``env_file`` fallback the way ``st_cli.config.Settings``
does for local CLI use — a GitHub Actions runner has no ``.env``, and ticket 05
requires credentials from environment only, so there must be no code path (now or
added later) that reads one.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from st_exporter.feeds.financial import DEFAULT_MAX_TIMESHEET_JOBS
from st_exporter.window import FINANCIAL_WINDOW_DAYS


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

    # How far back the `financial` feed reaches. A SEPARATE knob from
    # `window_days` on purpose: the two windows happen to share the number 90 but
    # not the reason, and one tenant needing a longer financial history (Profit
    # Wizard's reports page offers a 365-day timeframe) must not be able to drag
    # the jobs window along with it. See window.FINANCIAL_WINDOW_DAYS.
    # ge=1 for the same reason as window_days: a zero window would silently empty
    # three money tabs rather than error.
    financial_window_days: int = Field(
        default=FINANCIAL_WINDOW_DAYS, ge=1, validation_alias="EXPORTER_FINANCIAL_WINDOW_DAYS"
    )

    # Cap on how many completed jobs the `financial` feed asks for timesheets.
    # ServiceTitan has no bulk endpoint carrying the dispatch-shaped timesheet
    # fields Profit Wizard reads, so this costs one request per job — see
    # feeds/financial.py. ge=1 because zero would write an empty
    # `payroll.timesheets` tab that looks like "this tenant logs no labour".
    financial_max_jobs: int = Field(
        default=DEFAULT_MAX_TIMESHEET_JOBS, ge=1, validation_alias="EXPORTER_FINANCIAL_MAX_JOBS"
    )

    # Optional comma-separated ServiceTitan pricebook category ids to restrict the
    # pricebook feed to. Blank (the default) exports the whole catalogue, which is
    # what the tab contract describes. Kept as a raw string rather than a tuple
    # field because pydantic-settings parses a complex-typed env var as JSON, and
    # "1,2,3" is not JSON. Not GOOGLE_-prefixed; see window_days.
    #
    # Each id costs one extra serial request per item resource — ServiceTitan's
    # `categoryIds` filter honours exactly ONE id per request (see
    # feeds/pricebook.py), so ids are never batched.
    pricebook_category_ids_raw: str = Field(
        default="", validation_alias="EXPORTER_PRICEBOOK_CATEGORY_IDS"
    )

    @property
    def pricebook_category_ids(self) -> tuple[str, ...]:
        """``pricebook_category_ids_raw`` split into ids, blanks dropped."""
        parts = self.pricebook_category_ids_raw.split(",")
        return tuple(part.strip() for part in parts if part.strip())

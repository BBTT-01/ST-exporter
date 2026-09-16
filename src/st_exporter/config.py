"""Exporter settings, loaded from environment variables only.

Deliberately does not offer an ``env_file`` fallback the way ``st_cli.config.Settings``
does for local CLI use — a GitHub Actions runner has no ``.env``, and ticket 05
requires credentials from environment only, so there must be no code path (now or
added later) that reads one.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from st_exporter.feeds.contacts import (
    DEFAULT_MAX_CONTACT_CUSTOMERS,
    ROUTE_PER_CUSTOMER,
    ROUTES,
)
from st_exporter.feeds.financial import DEFAULT_MAX_TIMESHEET_JOBS
from st_exporter.images.upload import NO_ASSET_CAP
from st_exporter.window import FINANCIAL_WINDOW_DAYS

# The reusable workflow's own default `job_timeout_minutes`, repeated here so a
# local run and a CI run reason about the same clock.
DEFAULT_JOB_TIMEOUT_MINUTES = 10

# Taken off the job timeout before the image pass may start another asset. It
# has to cover what happens OUTSIDE this process — checkout, setup-python and
# `pip install -e .` all run before the exporter exists — and the ledger flush
# that happens after the pass. A minute pessimistic costs a few images; a minute
# optimistic costs the whole ledger to a SIGKILL, which is the failure this
# whole mechanism exists to prevent.
IMAGE_BUDGET_RESERVE_MINUTES = 2

# Never hand the image pass a budget smaller than this, however small the job
# timeout. Zero is not "run briefly", it is "never upload anything" — the bug
# rather than a configuration of it.
MIN_IMAGE_BUDGET_SECONDS = 60.0


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

    # Which route the jobs feed reads customer phone/email from. `per-customer`
    # (the default) is the one route with live evidence behind it — TradeRated's
    # Direct-path function has been reading `customers/{id}/contacts` in
    # production for both tenants. `export` opts into the bulk
    # `crm/export/customers/contacts` change-feed, which could not be shown to
    # exist without a live tenant to ask; if it answers 404/400 the run says so
    # and falls back to per-customer rather than blanking two columns. See
    # feeds/contacts.py. Not GOOGLE_-prefixed; see window_days.
    contacts_route: str = Field(
        default=ROUTE_PER_CUSTOMER, validation_alias="EXPORTER_CONTACTS_ROUTE"
    )

    # Cap on how many DISTINCT customers one run asks for contacts on the
    # per-customer route — the financial feed's EXPORTER_FINANCIAL_MAX_JOBS knob
    # for the same N+1 shape. ge=1 because zero would blank both contact columns
    # on every row, which is the bug this cap's feed exists to fix.
    contacts_max_customers: int = Field(
        default=DEFAULT_MAX_CONTACT_CUSTOMERS,
        ge=1,
        validation_alias="EXPORTER_CONTACTS_MAX_CUSTOMERS",
    )

    @field_validator("contacts_route")
    @classmethod
    def _known_contacts_route(cls, value: str) -> str:
        """Reject an unknown route rather than silently exporting blank columns.

        A typo'd `EXPORTER_CONTACTS_ROUTE` must not fall through to "fetch
        nothing": that is indistinguishable, on the tab, from the very bug this
        setting exists to fix.
        """
        route = value.strip().lower()
        if route not in ROUTES:
            raise ValueError(f"EXPORTER_CONTACTS_ROUTE must be one of {', '.join(ROUTES)}")
        return route

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

    # What the caller workflow's `timeout-minutes` is set to. The exporter is
    # TOLD rather than left to guess, because the runner does not warn before it
    # SIGKILLs the job and a killed process writes no image ledger: every upload
    # that run made is forgotten and re-sent by the next one, for ever, on any
    # tenant whose catalogue is bigger than one job. That is run 35130164187 on
    # `BBTT-01/tr-doorservpro` — killed at 10m35s, 0 images uploaded, every hour.
    # ge=1 because a job timeout of zero is not a thing GitHub accepts either.
    # Not GOOGLE_-prefixed; see window_days.
    job_timeout_minutes: int = Field(
        default=DEFAULT_JOB_TIMEOUT_MINUTES,
        ge=1,
        validation_alias="EXPORTER_JOB_TIMEOUT_MINUTES",
    )

    # Cap on how many assets ONE image run fetches over the network. 0 — the
    # default — is NO CAP, and that is deliberate: a number here would quietly
    # truncate a large catalogue for ever on every caller that never chose one,
    # and the symptom (a tenant permanently missing its last N images) looks
    # exactly like a clean run. A caller that wants the pass bounded sets a
    # number, and the run then says out loud when the cap bit
    # (`images_stopped=per-run asset cap reached`).
    #
    # `modifiedOn` skips are free and do not count against it — the cap bounds
    # WORK, not the scan — so a converged catalogue still sweeps end to end.
    # ge=0 rather than ge=1 because 0 is the sentinel, not a degenerate cap; a
    # cap of 1 would be legal and nearly useless, a cap of 0 meaning "fetch
    # nothing" would be the bug rather than a configuration of it.
    # Not GOOGLE_-prefixed; see window_days.
    image_max_assets: int = Field(
        default=NO_ASSET_CAP,
        ge=0,
        validation_alias="EXPORTER_IMAGE_MAX_ASSETS",
    )

    @property
    def image_budget_seconds(self) -> float:
        """How long, from the start of the run, the image pass may keep going.

        One knob, not two: a caller that raises `job_timeout_minutes` must not
        also have to remember a second number, and two numbers that can disagree
        would eventually disagree in the direction that loses the ledger.
        """
        budget = (self.job_timeout_minutes - IMAGE_BUDGET_RESERVE_MINUTES) * 60.0
        return max(MIN_IMAGE_BUDGET_SECONDS, budget)

    @property
    def pricebook_category_ids(self) -> tuple[str, ...]:
        """``pricebook_category_ids_raw`` split into ids, blanks dropped."""
        parts = self.pricebook_category_ids_raw.split(",")
        return tuple(part.strip() for part in parts if part.strip())

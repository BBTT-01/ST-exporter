"""Resolve-or-create the one marketing campaign every referral lead is filed against.

ServiceTitan refuses `POST crm/leads` without a `campaignId` — observed live on
2026-09-08 as `ServiceTitan 400: campaignId and summary required`, against a real
Door Serv Pro tenant. There is no campaign id anywhere in TradeRated's Outbox
payload and no per-company configuration that could carry one, so the exporter
resolves it itself: look for a campaign by name, create it on the first run that
needs one, and reuse it forever after.

Giving referrals their own campaign rather than filing them under an existing one is
deliberate. Campaign is the dimension ServiceTitan reports revenue by, so a shared
campaign would blur what referrals earn into the customer's other marketing spend —
the number the customer most wants out of this integration.

The lookup is cached for the lifetime of the resolver, which the drain constructs once
per run, so a batch of ten referrals costs one campaign lookup rather than ten.
"""

from __future__ import annotations

import os
from typing import Any

from st_cli.client import ServiceTitanClient
from st_cli.pagination import fetch_all
from st_exporter.logging_setup import logger

REFERRAL_CAMPAIGN_NAME = "TradeRated Referrals"

_PAGE_SIZE = 200

# Overrides, for correcting attribution without a release. A customer with several
# business units may want referrals booked against a specific one; ServiceTitan reports
# revenue by campaign, so that choice is theirs to make and ours to honour.
_ENV_BUSINESS_UNIT = "TRADERATED_CAMPAIGN_BUSINESS_UNIT_ID"
_ENV_CATEGORY = "TRADERATED_CAMPAIGN_CATEGORY_ID"


class CampaignResolutionError(Exception):
    """The referral campaign could neither be found nor created."""


class ReferralCampaign:
    """Lazily resolves ``REFERRAL_CAMPAIGN_NAME`` to a ServiceTitan campaign id."""

    def __init__(self, client: ServiceTitanClient, name: str = REFERRAL_CAMPAIGN_NAME) -> None:
        self._client = client
        self._name = name
        self._campaign_id: int | None = None

    def campaign_id(self) -> int:
        """The campaign id, resolving (and creating if needed) on first call.

        Cached including across items in one drain: the id cannot change mid-run, and
        re-listing per item would turn one referral batch into N full campaign lists.
        """
        if self._campaign_id is None:
            found = self._find()
            self._campaign_id = found if found is not None else self._create()
        return self._campaign_id

    def _find(self) -> int | None:
        """Match on name, case-insensitively, over the full campaign list.

        Deliberately not a server-side `name=` filter: whether ServiceTitan's campaign
        list supports one is unconfirmed, and a filter it silently ignores would return
        page one of every campaign and match the wrong row. Campaign counts are small
        (tens), so listing all of them is cheap and unambiguous.
        """
        wanted = self._name.casefold()
        for record in fetch_all(self._client, "marketing", "campaigns", page_size=_PAGE_SIZE):
            name = record.get("name")
            if isinstance(name, str) and name.casefold() == wanted and "id" in record:
                logger.info("referral campaign %r resolved to id %s", self._name, record["id"])
                return int(record["id"])
        return None

    def _create(self) -> int:
        """Create the campaign. Runs at most once per tenant, on the first referral.

        ServiceTitan requires more than a name. The first live attempt (2026-09-08) was
        refused with:

            categoryId:     Required property 'categoryId' not found
            businessUnitId: Required property 'businessUnitId' not found

        Both are resolved from the tenant rather than asked for at onboarding, so a new
        Hosted customer's first referral succeeds without anyone having configured
        anything. Each resolution is logged, because these two ids decide how the
        customer's own reporting attributes referral revenue — a silent default would be
        a silent misattribution.
        """
        business_unit_id = self._resolve_business_unit()
        category_id = self._resolve_category()

        body: dict[str, Any] = {
            "name": self._name,
            "active": True,
            "businessUnitId": business_unit_id,
            "categoryId": category_id,
        }
        logger.info(
            "creating referral campaign %r (businessUnitId=%s categoryId=%s)",
            self._name,
            business_unit_id,
            category_id,
        )
        try:
            created = self._client.post("marketing", "campaigns", json_body=body)
        except Exception as exc:
            raise CampaignResolutionError(
                f"could not create the {self._name!r} campaign, so no referral can be filed: {exc}"
            ) from exc

        campaign_id = created.get("id") if isinstance(created, dict) else None
        if campaign_id is None:
            raise CampaignResolutionError(
                f"ServiceTitan accepted the {self._name!r} campaign but returned no id: {created!r}"
            )
        logger.info("referral campaign %r created with id %s", self._name, campaign_id)
        return int(campaign_id)

    def _resolve_business_unit(self) -> int:
        """The business unit the campaign is booked against.

        Deterministic by lowest id among active units, so repeated runs and repeated
        customers behave the same way and the choice is reproducible from the log. A
        customer who wants a different one sets TRADERATED_CAMPAIGN_BUSINESS_UNIT_ID;
        nothing here needs changing for that.
        """
        override = self._override(_ENV_BUSINESS_UNIT)
        if override is not None:
            return override
        chosen = self._lowest_active_id("settings", "business-units")
        if chosen is None:
            raise CampaignResolutionError(
                f"the {self._name!r} campaign needs a businessUnitId and this tenant "
                f"reported no active business units; set {_ENV_BUSINESS_UNIT} to pin one"
            )
        logger.info("referral campaign business unit resolved to %s", chosen)
        return chosen

    def _resolve_category(self) -> int:
        """The marketing category the campaign is filed under.

        Same rule and same escape hatch as the business unit. Categories are not created
        here: creating one would need its own set of required fields, which is another
        unverified guess, and every tenant ServiceTitan provisions already has some.
        """
        override = self._override(_ENV_CATEGORY)
        if override is not None:
            return override
        chosen = self._lowest_active_id("marketing", "categories")
        if chosen is None:
            raise CampaignResolutionError(
                f"the {self._name!r} campaign needs a categoryId and this tenant "
                f"reported no active marketing categories; set {_ENV_CATEGORY} to pin one"
            )
        logger.info("referral campaign category resolved to %s", chosen)
        return chosen

    @staticmethod
    def _override(env_name: str) -> int | None:
        raw = (os.environ.get(env_name) or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise CampaignResolutionError(
                f"{env_name} must be a numeric ServiceTitan id, got {raw!r}"
            ) from exc
        logger.info("%s pinned to %s by configuration", env_name, value)
        return value

    def _lowest_active_id(self, module: str, resource: str) -> int | None:
        """Lowest id among records that are not explicitly inactive.

        `active` absent is treated as active: these list endpoints are not guaranteed to
        return the field, and excluding a record for a missing flag would be worse than
        including it — the alternative is failing to create the campaign at all.
        """
        ids = [
            int(record["id"])
            for record in fetch_all(self._client, module, resource, page_size=_PAGE_SIZE)
            if "id" in record and record.get("active", True)
        ]
        return min(ids) if ids else None

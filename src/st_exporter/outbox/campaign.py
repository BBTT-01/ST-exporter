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

from typing import Any

from st_cli.client import ServiceTitanClient
from st_cli.pagination import fetch_all
from st_exporter.logging_setup import logger

REFERRAL_CAMPAIGN_NAME = "TradeRated Referrals"

_PAGE_SIZE = 200


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

        The request body is a documented guess — see KNOWN_UNVERIFIED.md. If ServiceTitan
        requires more than a name (a business unit, a category, a DNIS), this raises with
        ServiceTitan's own message rather than a generic failure, so the drain reports a
        cause a human can act on instead of the opaque 400 this replaces.
        """
        body: dict[str, Any] = {"name": self._name, "active": True}
        logger.info("referral campaign %r not found; creating it", self._name)
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

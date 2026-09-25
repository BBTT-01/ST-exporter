"""One lane per product: claim from that app, write to ServiceTitan, report back.

A **lane** is everything that differs between the three outbox queues — the base
URL, the machine token, the path shape, the claim envelope, the ServiceTitan
write, and the result vocabulary — behind one small interface the drain loop can
hold. ``drain.py`` knows nothing about which app it is draining.

There is deliberately **no shared result vocabulary**. TradeRated settles a
credit hold on ``{"status": "succeeded", "st_id": ...}``; TrueQuote attaches a
booking to a lead on ``{"status": "succeeded", "booking_id": ...}`` and never
reads ``st_id`` at all. Collapsing those into one wire shape would mean one of
the two apps silently losing the id of the thing this exporter just created.

**Per-lane isolation.** ``drain.drain_lanes`` runs each lane inside its own
``try``, so a lane whose app is down, slow, 500ing or answering with garbage
costs exactly one lane. The caller (``cli.py``) wraps the whole thing again, so
no outbox failure can touch the export feeds either — by the time the drain
runs, every Sheet write has already been committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from st_cli.client import ServiceTitanClient
from st_exporter.logging_setup import logger
from st_exporter.outbox.actions import perform_item
from st_exporter.outbox.booking_provider import TrueQuoteBookingProvider
from st_exporter.outbox.booking_schedule import (
    TrueQuoteBookingSchedule,
    TrueQuoteBusinessUnit,
    booking_settings,
)
from st_exporter.outbox.campaign import ReferralCampaign
from st_exporter.outbox.client import OutboxItem, TradeRatedOutboxClient
from st_exporter.outbox.profitwizard import ProfitWizardOutboxClient, perform_profitwizard_item
from st_exporter.outbox.settings import PROFITWIZARD, TRADERATED, TRUEQUOTE, LaneCredentials
from st_exporter.outbox.truequote import TrueQuoteBookingOutboxClient, perform_booking


class OutboxLane(Protocol):
    """What ``drain.py`` needs from a product's queue."""

    @property
    def product(self) -> str: ...

    def claim(self, limit: int) -> list[OutboxItem]: ...

    def perform(self, client: ServiceTitanClient, item: OutboxItem) -> str: ...

    def report_success(self, item: OutboxItem, st_id: str) -> None: ...

    def report_failure(self, item: OutboxItem, error: str) -> None: ...

    def close(self) -> None: ...


class TradeRatedLane:
    """TradeRated's CRM Outbox: ``GET {base}/crm-outbox``, ``POST .../{id}/result``.

    ``{base}`` is the Supabase Functions origin — ``https://<ref>.supabase.co/
    functions/v1`` — under which the ``crm-outbox`` edge function is deployed
    (``supabase/functions/crm-outbox/index.ts``, ``verify_jwt = false``). The
    machine token's scope is ``crm_outbox``.

    One :class:`ReferralCampaign` is held for the lane's whole drain: the
    campaign cannot change mid-run, so ten referrals cost one lookup instead of
    ten. Constructed eagerly, resolved lazily, so a batch with no referral leads
    makes no marketing call at all.
    """

    def __init__(
        self,
        client: TradeRatedOutboxClient,
        campaign: ReferralCampaign,
        product: str = TRADERATED,
    ) -> None:
        self._client = client
        self._campaign = campaign
        # The product this lane REPORTS as — normally `traderated`, but a lane
        # borrowing this shape via `*_OUTBOX_DIALECT` keeps its own identity, so
        # its ledger rows and log lines never merge into TradeRated's.
        self.product = product

    @classmethod
    def build(cls, credentials: LaneCredentials, st_client: ServiceTitanClient) -> "TradeRatedLane":
        return cls(
            TradeRatedOutboxClient(
                credentials.base_url, credentials.machine_token, credentials.routes
            ),
            ReferralCampaign(st_client),
            credentials.product,
        )

    def claim(self, limit: int) -> list[OutboxItem]:
        return self._client.claim(limit=limit)

    def perform(self, client: ServiceTitanClient, item: OutboxItem) -> str:
        return perform_item(client, item, self._campaign)

    def report_success(self, item: OutboxItem, st_id: str) -> None:
        self._client.report_result(item.id, status="succeeded", st_id=st_id)

    def report_failure(self, item: OutboxItem, error: str) -> None:
        self._client.report_result(item.id, status="failed", error=error)

    def close(self) -> None:
        self._client.close()


class TrueQuoteLane:
    """TrueQuote's booking outbox: ``POST {base}/booking/claim`` and ``/result``.

    ``{base}`` is ``https://<host>/api/outbox`` — the same origin the pricebook
    image route hangs off — but the token is NOT the same one. TrueQuote mints a
    token per scope and answers a cross-scope token with a flat 401, so the
    ``booking_outbox`` token here and the ``image_upload`` token in
    ``TRADERATED_IMAGE_TOKEN`` are two separate secrets that happen to share a
    base URL.

    One :class:`TrueQuoteBookingProvider` is held for the lane's whole drain,
    exactly as TradeRated's lane holds its campaign: the tag is resolved on the
    first booking that carries no provider id, and never again this run. The
    business unit is held the same way.
    """

    def __init__(
        self,
        client: TrueQuoteBookingOutboxClient,
        provider: TrueQuoteBookingProvider,
        business_unit: TrueQuoteBusinessUnit,
        schedule: TrueQuoteBookingSchedule | None = None,
        product: str = TRUEQUOTE,
    ) -> None:
        self._client = client
        self._provider = provider
        self._business_unit = business_unit
        self._schedule = schedule or TrueQuoteBookingSchedule()
        # See TradeRatedLane.__init__ — a borrowed shape keeps its own identity.
        self.product = product

    @classmethod
    def build(cls, credentials: LaneCredentials, st_client: ServiceTitanClient) -> "TrueQuoteLane":
        business_unit_override, timezone_name = booking_settings()
        return cls(
            TrueQuoteBookingOutboxClient(
                credentials.base_url, credentials.machine_token, credentials.routes
            ),
            TrueQuoteBookingProvider(st_client),
            TrueQuoteBusinessUnit(st_client, business_unit_override),
            TrueQuoteBookingSchedule(timezone_name),
            credentials.product,
        )

    def claim(self, limit: int) -> list[OutboxItem]:
        return self._client.claim(limit=limit)

    def perform(self, client: ServiceTitanClient, item: OutboxItem) -> str:
        return perform_booking(client, item, self._provider, self._business_unit, self._schedule)

    def report_success(self, item: OutboxItem, st_id: str) -> None:
        self._client.report_success(item, st_id)

    def report_failure(self, item: OutboxItem, error: str) -> None:
        self._client.report_failure(item, error)

    def close(self) -> None:
        self._client.close()


class ProfitWizardLane:
    """Profit Wizard's ServiceTitan outbox: ``POST {base}/claim`` and ``/result``.

    A third base URL, a third path shape, a third token scope
    (``servicetitan_outbox``) and a fourth-and-fifth-and-sixth set of item kinds.
    The lane is real and drained; the four ServiceTitan writes its items carry
    belong to ticket 15 — see ``perform_profitwizard_item``.
    """

    def __init__(self, client: ProfitWizardOutboxClient, product: str = PROFITWIZARD) -> None:
        self._client = client
        self.product = product

    @classmethod
    def build(
        cls, credentials: LaneCredentials, st_client: ServiceTitanClient
    ) -> "ProfitWizardLane":
        del st_client  # this lane resolves nothing up front
        return cls(
            ProfitWizardOutboxClient(
                credentials.base_url, credentials.machine_token, credentials.routes
            ),
            credentials.product,
        )

    def claim(self, limit: int) -> list[OutboxItem]:
        return self._client.claim(limit=limit)

    def perform(self, client: ServiceTitanClient, item: OutboxItem) -> str:
        return perform_profitwizard_item(client, item)

    def report_success(self, item: OutboxItem, st_id: str) -> None:
        self._client.report_success(item, st_id)

    def report_failure(self, item: OutboxItem, error: str) -> None:
        self._client.report_failure(item, error)

    def close(self) -> None:
        self._client.close()


# The three lanes, each with its OWN routes and its OWN token scope. This table
# is the whole of what "which products can be drained" means; adding a fourth
# product is one entry here plus its client.
_LANE_BUILDERS: dict[str, type[TradeRatedLane] | type[TrueQuoteLane] | type[ProfitWizardLane]] = {
    TRADERATED: TradeRatedLane,
    TRUEQUOTE: TrueQuoteLane,
    PROFITWIZARD: ProfitWizardLane,
}


@dataclass(frozen=True)
class SkippedLane:
    """A product whose secrets are set but which cannot be drained, and why."""

    product: str
    reason: str


def build_lanes(
    credentials: list[LaneCredentials], st_client: ServiceTitanClient
) -> tuple[list[OutboxLane], list[SkippedLane]]:
    """Turn configured credentials into lanes, naming any that cannot be built."""
    lanes: list[OutboxLane] = []
    skipped: list[SkippedLane] = []

    for credential in credentials:
        builder = _LANE_BUILDERS.get(credential.product)
        if builder is None:
            skipped.append(
                SkippedLane(
                    product=credential.product,
                    reason=(
                        f"no outbox lane is implemented for {credential.product!r} "
                        f"(known: {', '.join(sorted(_LANE_BUILDERS))}). Its secrets are "
                        "accepted and ignored; nothing was claimed."
                    ),
                )
            )
            continue
        lanes.append(builder.build(credential, st_client))

    return lanes, skipped


def close_lanes(lanes: list[OutboxLane]) -> None:
    """Close every lane's HTTP client, never letting one failure skip the rest."""
    for lane in lanes:
        try:
            lane.close()
        except Exception as exc:
            logger.warning("closing the %s outbox client failed: %s", lane.product, exc)

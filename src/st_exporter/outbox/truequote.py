"""TrueQuote's booking outbox — claim, perform, report.

A different app, a different path shape, a different result vocabulary. None of
it is shared with TradeRated's lane and none of it should be: they are two
products with two databases and two contracts, and the only thing they have in
common is that one exporter drains both.

    POST {base}/booking/claim    {"limit": n}
    POST {base}/booking/result   one report, or a batch

``{base}`` is the same origin the pricebook image route hangs off —
``https://<host>/api/outbox`` — which is what makes ``{base}/pricebook-image``
and ``{base}/booking/claim`` both correct at once. Authenticated with a Machine
Token of scope ``booking_outbox``; the ``image_upload`` token earns a flat 401
(``machine-token.ts:153``), so the two are never interchangeable.

**The claim response is not TradeRated's.** Transcribed from
``apps/admin/app/api/outbox/booking/claim/route.ts:22-35``: the id field is
``item_id``, the payload field is ``booking``, there is no ``kind`` (the queue
is single-purpose — every row is a ServiceTitan booking), and each item also
carries ``tenant_id`` and, for a company that still has one configured,
``booking_provider_id``. A Hosted company sends none: the runner files its
bookings under its own ``TrueQuote`` Booking Provider Tag instead. Items are leased, not
deleted: ``lease_seconds`` (300) after a claim an unreported item is handed out
again, which is the at-least-once redelivery the ledger exists to absorb.

**The result contract WIDENS and never narrows** — their own words, at
``result/route.ts:7-13``. ``booking-result-report.ts`` reads the outcome from
any of ``status``/``state``/``outcome``/``result``/``disposition``, or booleans
``ok``/``success``/``succeeded``, and accepts a long list of success and failure
words. This client sends the narrowest thing that is unambiguous under that
parser and sends BOTH identifiers, because either alone is enough for them to
find the row and sending both survives one of them being dropped.

**``st_id`` is not a field they read.** Their id keys are ``booking_id`` /
``servicetitan_booking_id`` / ``external_id`` and friends
(``booking-result-report.ts:50-60``); a ``st_id`` would be silently ignored and
the booking id would never land on the lead. This is the single most important
difference from TradeRated's lane, and the reason a shared result vocabulary
across apps would have been a bug rather than a tidy-up.

**Replays are safe on their side too**: ``applyBookingResult`` only moves an
item out of ``pending``/``claimed``, so a second identical report writes nothing
and answers ``status: "duplicate"`` with a 200.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from st_cli.client import ServiceTitanClient
from st_exporter.outbox.booking_provider import TrueQuoteBookingProvider
from st_exporter.outbox.booking_schedule import TrueQuoteBookingSchedule, TrueQuoteBusinessUnit
from st_exporter.outbox.client import OutboxItem, drop_unidentified
from st_exporter.outbox.routes import TRUEQUOTE_ROUTES, LaneRoutes

_DEFAULT_TIMEOUT = 30.0

# Their queue is single-purpose and sends no discriminator, so the exporter
# supplies one. It is what lands in the ledger's `kind` column and in the log,
# and it must never collide with a TradeRated kind.
BOOKING_KIND = "booking"

# `createBookingPayload`, packages/servicetitan/src/server.ts:840. ServiceTitan's
# Bookings API requires `source`, `name` and `summary`, and ignores flat
# phone/email fields — contact methods must be a `contacts` array.
_BOOKING_SOURCE = "TrueQuote"
_FALLBACK_NAME = "TrueQuote website lead"


class TrueQuoteBookingOutboxClient:
    """Wraps ``POST {base}/booking/claim`` and ``POST {base}/booking/result``."""

    def __init__(
        self,
        base_url: str,
        machine_token: str,
        routes: LaneRoutes = TRUEQUOTE_ROUTES,
    ) -> None:
        self._routes = routes
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=_DEFAULT_TIMEOUT,
            headers={"Authorization": f"Bearer {machine_token}"},
        )

    def close(self) -> None:
        self._http.close()

    def claim(self, limit: int = 10) -> list[OutboxItem]:
        # A JSON BODY, not a query string — `readLimit` reads `request.json()`
        # (claim/route.ts:38-43). An absent or unparseable body falls back to
        # their default of 10, so sending it wrong would silently ignore our
        # limit rather than error.
        resp = self._http.post(self._routes.claim_path, json={"limit": limit})
        resp.raise_for_status()
        body = resp.json()
        items = (body or {}).get("items") or []
        # Same refusal the Profit Wizard lane makes: an item with a blank
        # `item_id` or `idempotency_key` is not an item. Keeping one would give
        # the ledger a `("truequote", "")` key that every other blank-keyed item
        # collides with, so the second customer's booking is reported delivered
        # with the FIRST one's booking id and never created.
        return drop_unidentified([_to_item(item) for item in items], "truequote")

    def report_success(self, item: OutboxItem, booking_id: str) -> None:
        self._report(item, {"status": "succeeded", "booking_id": booking_id})

    def report_failure(self, item: OutboxItem, error: str) -> None:
        self._report(item, {"status": "failed", "error": error})

    def _report(self, item: OutboxItem, outcome: dict[str, Any]) -> None:
        body = {
            "item_id": item.id,
            "idempotency_key": item.idempotency_key,
            **outcome,
        }
        resp = self._http.post(self._routes.result_path_for(item.id), json=body)
        resp.raise_for_status()


def _to_item(raw: dict[str, Any]) -> OutboxItem:
    return OutboxItem(
        id=str(raw.get("item_id") or ""),
        idempotency_key=str(raw.get("idempotency_key") or ""),
        kind=BOOKING_KIND,
        payload=raw.get("booking") or {},
        # `tenant_id` is deliberately NOT carried into the ServiceTitan call.
        # The runner has exactly one tenant — its own ST_TENANT_ID — and taking
        # a tenant id from the queue would let a value in TrueQuote's database
        # redirect a write into somebody else's ServiceTitan account.
        extra={"booking_provider_id": raw.get("booking_provider_id")},
    )


def perform_booking(
    client: ServiceTitanClient,
    item: OutboxItem,
    provider: TrueQuoteBookingProvider,
    business_unit: TrueQuoteBusinessUnit,
    schedule: TrueQuoteBookingSchedule,
    now: datetime | None = None,
) -> str:
    """Create the ServiceTitan booking ``item`` describes; return its id.

    The booking provider is the item's own ``booking_provider_id`` when it
    carries one (direct-era rows, and any company still configured with an id),
    and otherwise the runner's ``TrueQuote`` Booking Provider Tag, found or
    created once per run by ``provider`` (``booking_provider.py``).

    ``item.payload`` is TrueQuote's OWN ``ServiceTitanBookingInput`` — the thing
    `dispatch.ts:240` enqueues — not a ServiceTitan request body. The transform
    below is `createBookingPayload` (server.ts:840) re-expressed here, because
    the direct path applies it after the queue, at push time, and for a Hosted
    company the push happens on this runner instead. Forwarding the payload
    untouched would post camelCase junk and lose every phone number, which is
    the same mistake `referral_lead` made on 2026-09-08.

    The runner adds ``start`` and ``businessUnitId`` (``booking_schedule.py``),
    resolved before the provider tag so a booking that cannot be scheduled never
    creates a tag or posts.
    """
    body = build_booking_body(item.payload)
    body["start"] = schedule.start(item.payload, now or datetime.now(timezone.utc))
    body["businessUnitId"] = business_unit.unit_id()

    provider_id = item.extra.get("booking_provider_id") or provider.tag_id()
    created = client.post("crm", f"booking-provider/{provider_id}/bookings", json_body=body)
    return str(created["id"])


def build_booking_body(payload: dict[str, Any]) -> dict[str, Any]:
    """TrueQuote's ``ServiceTitanBookingInput`` -> ServiceTitan's booking body."""
    contacts = [
        {"type": type_, "value": str(payload[key])}
        for type_, key in (("Phone", "phone"), ("Email", "email"))
        if payload.get(key)
    ]

    name = str(payload.get("name") or "").strip() or _FALLBACK_NAME
    summary = str(payload.get("summary") or "").strip() or _fallback_summary(payload)

    body: dict[str, Any] = {
        "source": _BOOKING_SOURCE,
        "externalId": payload.get("sessionId"),
        "name": name,
        "summary": summary,
        "isFirstTimeClient": True,
    }
    if contacts:
        body["contacts"] = contacts

    address = payload.get("address") or {}
    if address.get("street"):
        body["address"] = {
            "street": address["street"],
            **{key: address[key] for key in ("unit", "city", "state", "zip") if address.get(key)},
            # Their default, not ours: `createBookingPayload` hard-codes USA
            # when the widget collected no country.
            "country": address.get("country") or "USA",
        }

    return body


def _fallback_summary(payload: dict[str, Any]) -> str:
    """`createSummary`, server.ts — used only when TrueQuote sent no summary.

    In practice they always do (`buildServiceTitanBookingInput` sets it from
    `buildCrmNote`), but ServiceTitan REJECTS a booking with no summary, so a
    lead must not be lost to a field one of their code paths forgot to fill.
    """
    parts = ["Quote request"]
    trade = payload.get("trade")
    if trade:
        parts.append(str(trade))
    min_price, max_price = payload.get("minPrice"), payload.get("maxPrice")
    if min_price is not None and max_price is not None:
        parts.append(f"range ${min_price}-{max_price}")
    summary = ": ".join(parts)

    extras: list[str] = []
    quote_summary = payload.get("quoteSummary")
    if quote_summary:
        extras.append(f"Configuration: {', '.join(str(part) for part in quote_summary)}")
    for label, key in (
        ("Door preview", "renderUrl"),
        ("Preferred time", "preferredTime"),
        ("Notes", "notes"),
    ):
        value = payload.get(key)
        if value:
            extras.append(f"{label}: {value}")

    return f"{summary}. {'. '.join(extras)}" if extras else summary

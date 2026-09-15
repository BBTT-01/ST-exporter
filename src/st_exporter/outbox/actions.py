"""Dispatch: perform one Outbox item against ServiceTitan.

Kinds come from TradeRated's Outbox contract (spec.md, "Outbox contract"):
`referral_lead` and `technician_rating`. Each performer returns the ServiceTitan
id created/affected, which is what gets reported back so TradeRated can link its
own record to a real ServiceTitan entity.

`technician_rating` used to raise here, on the claim that no ServiceTitan write
endpoint existed. That claim was false when it was written: `registry.py` has
declared `Module("customer-interactions", resources=(Resource("technician-ratings",
ops="LRC"),))` since 2026-06-01 — three months before this file — and `C` is
create. The URL that generates, `/customer-interactions/v2/tenant/{id}/
technician-ratings`, is character-for-character the one TradeRated's own Direct
path posts to (`update-servicetitan-rating/index.ts:88`), and the scope for it is
already granted: `SETUP.md` has every contractor tick Customer Interactions ->
Technician Rating -> WRITE before their first run. Nothing was missing but the
code, so every rating on a Hosted company was reported failed against a
permission the contractor had already given.

Only genuinely unknown kinds raise now.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from st_cli.client import ServiceTitanClient
from st_exporter.outbox.campaign import ReferralCampaign
from st_exporter.outbox.client import OutboxItem


class UnsupportedOutboxKindError(Exception):
    """Raised for an Outbox kind this exporter has no ServiceTitan write for yet."""


def perform_item(
    client: ServiceTitanClient,
    item: OutboxItem,
    campaign: ReferralCampaign | None = None,
) -> str:
    """Perform ``item`` against ServiceTitan; return the resulting ServiceTitan id.

    ``campaign`` is shared across a whole drain so the campaign is resolved once per
    run rather than once per referral; one is constructed here if the caller omits it,
    which keeps single-item callers and tests simple.

    Raises ``UnsupportedOutboxKindError`` for a kind with no known implementation —
    callers must catch this and report a failure rather than let it crash the run
    (see ``drain.py``).
    """
    if item.kind == "referral_lead":
        return _perform_referral_lead(client, item, campaign or ReferralCampaign(client))
    if item.kind == "technician_rating":
        return _perform_technician_rating(client, item)
    raise UnsupportedOutboxKindError(f"unknown outbox kind: {item.kind!r}")


def _perform_referral_lead(
    client: ServiceTitanClient,
    item: OutboxItem,
    campaign: ReferralCampaign,
) -> str:
    """Create a CRM lead from ``item.payload``.

    Earlier versions passed the payload through untouched, on the assumption that it
    already matched ServiceTitan's lead-creation body. It does not, and the first real
    attempt proved it: `ServiceTitan 400: campaignId and summary required`, 2026-09-08.
    TradeRated's payload is its own snake_case shape (`name`, `phone`, `address`,
    `referred_by`) and carries neither required field, so the exporter now supplies both.

    `followUpDate` is the third field ServiceTitan demands, discovered the same way:

        400: Follow up date or Call Reason ID is required.

    It is an either/or with `callReasonId`, so this sets neither when TradeRated has
    already sent one of them. `followUpDate` is the half chosen because it needs no
    further lookup and no further scope — `callReasonId` would require reading
    `crm/call-reasons`, which is very likely un-granted, exactly like `marketing/categories`.

    Only ABSENT keys are filled. A `campaignId`, `summary` or follow-up TradeRated chooses
    to send wins, so this cannot silently override a future decision made on that side.
    """
    body = dict(item.payload)
    # Explicit membership tests, NOT `setdefault`: setdefault evaluates its default
    # eagerly, so it would resolve (and on a fresh tenant CREATE) the campaign even when
    # TradeRated had already supplied one. Covered by
    # `test_a_campaign_id_from_traderated_is_not_overridden`.
    if "campaignId" not in body:
        body["campaignId"] = campaign.campaign_id()
    if "summary" not in body:
        body["summary"] = _summary(item.payload)
    if "followUpDate" not in body and "callReasonId" not in body:
        body["followUpDate"] = _today().isoformat()

    created = client.post("crm", "leads", json_body=body)
    return str(created["id"])


def _perform_technician_rating(client: ServiceTitanClient, item: OutboxItem) -> str:
    """Post one technician rating; return a synthetic id for the pair it keys on.

    TradeRated's payload (``update-servicetitan-rating/index.ts:251-259``) is
    ``{review_id, rating, servicetitan_job_id, servicetitan_technician_id,
    customer_name}``, where ``rating`` is the raw **1-5 star** review value and
    both ids are **strings**.

    ServiceTitan's body is ``{technicianId, jobId, rating}`` with integer ids and
    a rating on a **0-10** scale. The star-to-ten conversion is `convertRating`
    (same file, lines 117-120): ``rating * 2``. Forwarding the payload unmapped
    would post every five-star review as 5/10 — a wrong number that looks right,
    which nobody would notice for weeks. It is the single reason this function
    exists rather than a pass-through.

    The endpoint is create-or-update keyed on (technician, job) and its response
    carries no id of its own, so the reported ``st_id`` is that pair. That also
    makes an at-least-once redelivery harmless on ServiceTitan's side: the same
    rating re-posted overwrites itself with the same value.
    """
    payload = item.payload
    try:
        technician_id = int(payload["servicetitan_technician_id"])
        job_id = int(payload["servicetitan_job_id"])
    except (KeyError, TypeError, ValueError) as exc:
        # A rating with no ST job or technician id cannot be placed anywhere.
        # Raised as a plain failure (not UnsupportedOutboxKindError): the KIND is
        # supported, this one item is unusable, and the distinction is what tells
        # a reader whether to fix the exporter or fix the row.
        raise ValueError(
            "technician_rating needs integer servicetitan_technician_id and "
            f"servicetitan_job_id; got {payload.get('servicetitan_technician_id')!r} "
            f"and {payload.get('servicetitan_job_id')!r}"
        ) from exc

    body = {
        "technicianId": technician_id,
        "jobId": job_id,
        "rating": _star_rating_to_servicetitan(payload.get("rating")),
    }
    client.post("customer-interactions", "technician-ratings", json_body=body)
    return f"{technician_id}:{job_id}"


def _star_rating_to_servicetitan(stars: Any) -> float:
    """1-5 stars -> ServiceTitan's 0-10 double, clamped to the range it accepts.

    Clamped rather than validated: a rating is not worth failing an item over,
    and a value outside 1-5 means TradeRated changed its scale, in which case
    posting the nearest legal number beats reporting failed five times.
    """
    try:
        value = float(stars) * 2
    except (TypeError, ValueError) as exc:
        raise ValueError(f"technician_rating carried a non-numeric rating: {stars!r}") from exc
    return max(0.0, min(10.0, value))


def _today() -> date:
    """Today in UTC. Separate function so tests can pin it.

    A referral is warmest the moment it arrives, so the follow-up date is today rather
    than a future date: it lands in the customer's Follow Ups list immediately instead of
    after someone else has already called the homeowner.

    UTC, not the customer's timezone, because the exporter has no access to it — a
    referral queued late in the US evening therefore gets tomorrow's date. That shifts
    the follow-up by one day at worst and is the reason this is a named function rather
    than an inline call.
    """
    return datetime.now(timezone.utc).date()


def _summary(payload: dict[str, Any]) -> str:
    """One line of human-readable text for the lead's `summary` field.

    ServiceTitan requires it, and it is the only field on a bare lead a person actually
    reads in the UI — so the referral's contact details go here rather than being left
    to whichever of TradeRated's snake_case keys ServiceTitan happens to ignore. Losing
    the name and phone number would make the lead useless to the office staff who work it.
    """
    parts = ["Referral from TradeRated"]

    name = payload.get("name")
    if name:
        parts.append(f"for {name}")

    referred_by = payload.get("referred_by")
    if referred_by:
        parts.append(f"referred by {referred_by}")

    contact = [str(payload[key]) for key in ("phone", "email") if payload.get(key)]
    if contact:
        parts.append(f"({', '.join(contact)})")

    notes = payload.get("notes")
    if notes:
        parts.append(f"\u2014 {notes}")

    return " ".join(parts)

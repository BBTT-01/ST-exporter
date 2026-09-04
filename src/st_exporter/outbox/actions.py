"""Dispatch: perform one Outbox item against ServiceTitan.

Kinds come from TradeRated's Outbox contract (spec.md, "Outbox contract"):
`referral_lead` and `technician_rating`. Each performer returns the ServiceTitan
id created/affected, which is what gets reported back so TradeRated can link its
own record to a real ServiceTitan entity.

`technician_rating` has no known ServiceTitan write endpoint anywhere in this
CLI's registry — ServiceTitan has no obvious native "post a rating" concept.
Rather than guess (a note? a custom field? something else?), it's raised here as
an explicit, distinguishable failure so the drain loop reports it back to
TradeRated as failed (releasing any credit hold) instead of silently dropping it
or inventing a write nobody asked for. Flagged in this ticket's final report as
an open question for the spec owner.
"""

from __future__ import annotations

from st_cli.client import ServiceTitanClient
from st_exporter.outbox.client import OutboxItem


class UnsupportedOutboxKindError(Exception):
    """Raised for an Outbox kind this exporter has no ServiceTitan write for yet."""


def perform_item(client: ServiceTitanClient, item: OutboxItem) -> str:
    """Perform ``item`` against ServiceTitan; return the resulting ServiceTitan id.

    Raises ``UnsupportedOutboxKindError`` for a kind with no known implementation —
    callers must catch this and report a failure rather than let it crash the run
    (see ``drain.py``).
    """
    if item.kind == "referral_lead":
        return _perform_referral_lead(client, item)
    if item.kind == "technician_rating":
        raise UnsupportedOutboxKindError(
            "technician_rating has no known ServiceTitan write endpoint yet "
            "(no rating concept in this CLI's registry) — needs clarification "
            "from the spec owner, not a guess."
        )
    raise UnsupportedOutboxKindError(f"unknown outbox kind: {item.kind!r}")


def _perform_referral_lead(client: ServiceTitanClient, item: OutboxItem) -> str:
    """Create a CRM lead from ``item.payload``.

    The payload's shape is TradeRated's to define (issue 04, not this repo's
    scope) and is assumed to already match ServiceTitan's lead-creation body —
    passed through as-is rather than remapped field-by-field, since remapping
    unknown fields would be guessing at a contract this repo doesn't own.
    """
    created = client.post("crm", "leads", json_body=item.payload)
    return str(created["id"])

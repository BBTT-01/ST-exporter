"""Drains TradeRated's CRM Outbox in the same run as the jobs/technicians export.

At-least-once delivery (spec.md) means a redelivered item must not double-write
to ServiceTitan — ``ledger.py``'s ``_outbox_ledger`` tab is what makes a retry
after a mid-run crash safe: an item already recorded there is only re-reported,
never re-performed.

That property only holds if the ledger row is durable *before* anything else
can go wrong, so this loop flushes per item, immediately after recording and
before reporting — not once at the end, which would lose every already-performed
item in the batch to a crash or to one item's report failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from st_cli.client import ServiceTitanClient
from st_exporter.logging_setup import logger
from st_exporter.outbox.actions import UnsupportedOutboxKindError, perform_item
from st_exporter.outbox.campaign import ReferralCampaign
from st_exporter.outbox.client import TradeRatedOutboxClient
from st_exporter.outbox.ledger import LedgerEntry, OutboxLedger

_DEFAULT_CLAIM_LIMIT = 10


@dataclass
class DrainSummary:
    claimed: int
    # Counts ServiceTitan writes that happened, not reports that landed: an item
    # performed and recorded whose result-report to TradeRated then failed is
    # still `succeeded` here — the write is real and durable either way.
    succeeded: int
    failed: int
    replayed: int  # already in the ledger; re-reported without a new ST write


def _report(
    outbox_client: TradeRatedOutboxClient,
    item_id: str,
    *,
    status: Literal["succeeded", "failed"],
    st_id: str | None = None,
    error: str | None = None,
) -> None:
    """Report one item's outcome, swallowing transport failures.

    Reporting is the *recoverable* half of the drain: by the time it runs, any
    ServiceTitan write has already happened and is already durable in the
    ledger, so a TradeRated 5xx/timeout here costs nothing more than a
    redelivery — which the ledger turns into a clean idempotent re-report next
    run. Letting it propagate, by contrast, would abandon every remaining item
    in the batch, so it is deliberately caught and logged instead.
    """
    kwargs: dict[str, Any] = {"status": status}
    if st_id is not None:
        kwargs["st_id"] = st_id
    if error is not None:
        kwargs["error"] = error
    try:
        outbox_client.report_result(item_id, **kwargs)
    except Exception as exc:
        logger.warning(
            "outbox item %s: reporting %s result to TradeRated failed: %s", item_id, status, exc
        )


def drain_outbox(
    client: ServiceTitanClient,
    outbox_client: TradeRatedOutboxClient,
    ledger: OutboxLedger,
    *,
    limit: int = _DEFAULT_CLAIM_LIMIT,
) -> DrainSummary:
    items = outbox_client.claim(limit=limit)
    succeeded = failed = replayed = 0
    # One resolver for the whole batch: the referral campaign cannot change mid-run, so
    # ten referrals cost one campaign lookup instead of ten. Constructed unconditionally
    # but resolved lazily, so a batch with no referral leads makes no marketing call.
    campaign = ReferralCampaign(client)

    for item in items:
        existing = ledger.get(item.idempotency_key)
        if existing is not None:
            replayed += 1
            logger.info("outbox item %s already performed (idempotency replay)", item.id)
            _report(outbox_client, item.id, status="succeeded", st_id=existing.st_id)
            continue

        try:
            st_id = perform_item(client, item, campaign)
        except UnsupportedOutboxKindError as exc:
            failed += 1
            logger.warning("outbox item %s (%s) not performed: %s", item.id, item.kind, exc)
            _report(outbox_client, item.id, status="failed", error=str(exc))
            continue
        except Exception as exc:  # one bad item must not kill the rest of the batch
            failed += 1
            logger.warning("outbox item %s (%s) failed: %s", item.id, item.kind, exc)
            _report(outbox_client, item.id, status="failed", error=str(exc))
            continue

        # Record AND flush before reporting: the ServiceTitan write has already
        # happened, so from this instant on the only thing that keeps a
        # redelivery from double-writing is this row being durable in the Sheet.
        # Flushing per item (at most ~10 per drain, given the claim limit) costs
        # a handful of extra Sheets writes and buys the crash-safety property
        # ledger.py exists for: a process death here, or a report_result raising
        # for any later item in the batch, can no longer lose it.
        ledger.record(
            LedgerEntry(
                idempotency_key=item.idempotency_key,
                kind=item.kind,
                st_id=st_id,
                performed_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        ledger.flush()
        succeeded += 1
        _report(outbox_client, item.id, status="succeeded", st_id=st_id)

    # Safety net only — every recorded item was already flushed above, and
    # OutboxLedger.flush() is a no-op when nothing was ever loaded.
    ledger.flush()
    logger.info(
        "outbox drain: claimed=%d succeeded=%d failed=%d replayed=%d",
        len(items),
        succeeded,
        failed,
        replayed,
    )
    return DrainSummary(claimed=len(items), succeeded=succeeded, failed=failed, replayed=replayed)

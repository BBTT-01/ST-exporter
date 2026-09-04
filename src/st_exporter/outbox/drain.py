"""Drains TradeRated's CRM Outbox in the same run as the jobs/technicians export.

At-least-once delivery (spec.md) means a redelivered item must not double-write
to ServiceTitan — ``ledger.py``'s ``_outbox_ledger`` tab is what makes a retry
after a mid-run crash safe: an item already recorded there is only re-reported,
never re-performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from st_cli.client import ServiceTitanClient
from st_exporter.logging_setup import logger
from st_exporter.outbox.actions import UnsupportedOutboxKindError, perform_item
from st_exporter.outbox.client import TradeRatedOutboxClient
from st_exporter.outbox.ledger import LedgerEntry, OutboxLedger

_DEFAULT_CLAIM_LIMIT = 10


@dataclass
class DrainSummary:
    claimed: int
    succeeded: int
    failed: int
    replayed: int  # already in the ledger; re-reported without a new ST write


def drain_outbox(
    client: ServiceTitanClient,
    outbox_client: TradeRatedOutboxClient,
    ledger: OutboxLedger,
    *,
    limit: int = _DEFAULT_CLAIM_LIMIT,
) -> DrainSummary:
    items = outbox_client.claim(limit=limit)
    succeeded = failed = replayed = 0

    for item in items:
        existing = ledger.get(item.idempotency_key)
        if existing is not None:
            replayed += 1
            outbox_client.report_result(item.id, status="succeeded", st_id=existing.st_id)
            logger.info("outbox item %s already performed (idempotency replay)", item.id)
            continue

        try:
            st_id = perform_item(client, item)
        except UnsupportedOutboxKindError as exc:
            failed += 1
            logger.warning("outbox item %s (%s) not performed: %s", item.id, item.kind, exc)
            outbox_client.report_result(item.id, status="failed", error=str(exc))
            continue
        except Exception as exc:  # one bad item must not kill the rest of the batch
            failed += 1
            logger.warning("outbox item %s (%s) failed: %s", item.id, item.kind, exc)
            outbox_client.report_result(item.id, status="failed", error=str(exc))
            continue

        ledger.record(
            LedgerEntry(
                idempotency_key=item.idempotency_key,
                kind=item.kind,
                st_id=st_id,
                performed_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        outbox_client.report_result(item.id, status="succeeded", st_id=st_id)
        succeeded += 1

    ledger.flush()
    logger.info(
        "outbox drain: claimed=%d succeeded=%d failed=%d replayed=%d",
        len(items),
        succeeded,
        failed,
        replayed,
    )
    return DrainSummary(claimed=len(items), succeeded=succeeded, failed=failed, replayed=replayed)

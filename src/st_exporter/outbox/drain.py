"""Drains each configured product's outbox in the same run as the export feeds.

At-least-once delivery means a redelivered item must not double-write to
ServiceTitan — ``ledger.py``'s ``_outbox_ledger`` tab is what makes a retry
after a mid-run crash safe: an item already recorded there is only re-reported,
never re-performed. Its key is ``(product, idempotency_key)``, so three apps
minting keys independently cannot mask each other's work.

That property only holds if the ledger row is durable *before* anything else
can go wrong, so this loop flushes per item, immediately after recording and
before reporting — not once at the end, which would lose every already-performed
item in the batch to a crash or to one item's report failing.

**Never leave a performed item unreported.** The ledger is a best-effort
optimisation for recognising a redelivery; the app's own queue is the other half
of the same guarantee. So if the flush itself fails after a real ServiceTitan
write, the item is still reported succeeded — the app then stops redelivering it
— and the lane performs nothing further this run. Both records failing at once
is the only way a write duplicates, and that is strictly better than the flush
exception escaping, which leaves the item in neither.

Nothing here knows which app it is draining. Everything product-specific —
paths, claim envelope, ServiceTitan write, result vocabulary — lives behind
``lanes.OutboxLane``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from st_cli.client import ServiceTitanClient
from st_exporter.logging_setup import logger
from st_exporter.outbox.actions import UnsupportedOutboxKindError
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.lanes import OutboxLane
from st_exporter.outbox.ledger import LedgerEntry, OutboxLedger

_DEFAULT_CLAIM_LIMIT = 10


@dataclass
class DrainSummary:
    claimed: int
    # Counts ServiceTitan writes that happened, not reports that landed: an item
    # performed and recorded whose result-report to the app then failed is
    # still `succeeded` here — the write is real and durable either way.
    succeeded: int
    failed: int
    replayed: int  # already in the ledger; re-reported without a new ST write


@dataclass
class LaneOutcome:
    """What one lane did — or, if it blew up, what stopped it.

    ``summary`` and ``error`` are mutually exclusive and exactly one is set: a
    lane either ran to completion (possibly with per-item failures counted
    inside ``summary``) or it did not run at all.
    """

    product: str
    summary: DrainSummary | None = None
    error: str | None = None


def _report(lane: OutboxLane, item: OutboxItem, *, succeeded: bool, value: str) -> None:
    """Report one item's outcome, swallowing transport failures.

    Reporting is the *recoverable* half of the drain: by the time it runs, any
    ServiceTitan write has already happened and is already durable in the
    ledger, so a 5xx/timeout from the app here costs nothing more than a
    redelivery — which the ledger turns into a clean idempotent re-report next
    run. Letting it propagate, by contrast, would abandon every remaining item
    in the batch AND every remaining lane, so it is caught and logged instead.
    """
    try:
        if succeeded:
            lane.report_success(item, value)
        else:
            lane.report_failure(item, value)
    except Exception as exc:
        logger.warning(
            "outbox item %s: reporting %s result to %s failed: %s",
            item.id,
            "succeeded" if succeeded else "failed",
            lane.product,
            exc,
        )


def drain_outbox(
    client: ServiceTitanClient,
    lane: OutboxLane,
    ledger: OutboxLedger,
    *,
    limit: int = _DEFAULT_CLAIM_LIMIT,
) -> DrainSummary:
    items = lane.claim(limit)
    succeeded = failed = replayed = 0
    ledger_unwritable = False

    for item in items:
        if not item.idempotency_key or not item.id:
            # **An item with no identity is never performed.** The ledger key is
            # `(product, idempotency_key)`, so two blank-keyed items are ONE item
            # to it: the first is performed and recorded under `(product, "")`,
            # and every later one looks like a replay and is reported succeeded
            # *with the first one's ServiceTitan id*. That second customer's
            # booking is reported delivered and never created — a silently lost
            # write, which is the worst outcome this package has.
            #
            # Each lane's client already refuses these at claim time; this is the
            # backstop that makes it true for every lane, including one added
            # later that forgets. Not reported either way: with a blank `id`
            # there is nowhere to report TO, and nothing was performed, so the
            # app's lease simply expires and it redelivers — which is correct.
            failed += 1
            logger.error(
                "%s outbox item (id=%r, idempotency_key=%r) has no identity and was NOT "
                "performed. A blank idempotency key collides with every other blank one "
                "in the ledger and would report a write that never happened as delivered. "
                "This means the claim envelope is being read wrong.",
                lane.product,
                item.id,
                item.idempotency_key,
            )
            continue

        existing = ledger.get(item.idempotency_key, lane.product)
        if existing is not None:
            replayed += 1
            logger.info(
                "%s outbox item %s already performed (idempotency replay)",
                lane.product,
                item.id,
            )
            _report(lane, item, succeeded=True, value=existing.st_id)
            continue

        try:
            st_id = lane.perform(client, item)
        except UnsupportedOutboxKindError as exc:
            failed += 1
            logger.warning(
                "%s outbox item %s (%s) not performed: %s", lane.product, item.id, item.kind, exc
            )
            _report(lane, item, succeeded=False, value=str(exc))
            continue
        except Exception as exc:  # one bad item must not kill the rest of the batch
            failed += 1
            logger.warning(
                "%s outbox item %s (%s) failed: %s", lane.product, item.id, item.kind, exc
            )
            _report(lane, item, succeeded=False, value=str(exc))
            continue

        # Record AND flush before reporting: the ServiceTitan write has already
        # happened, so from this instant on the only thing that keeps a
        # redelivery from double-writing is this row being durable in the Sheet.
        # Flushing per item (at most ~10 per lane, given the claim limit) costs
        # a handful of extra Sheets writes and buys the crash-safety property
        # ledger.py exists for: a process death here, or a report failing for
        # any later item in the batch, can no longer lose it.
        ledger.record(
            LedgerEntry(
                idempotency_key=item.idempotency_key,
                kind=item.kind,
                st_id=st_id,
                performed_at=datetime.now(timezone.utc).isoformat(),
                product=lane.product,
            )
        )
        succeeded += 1
        try:
            ledger.flush()
        except Exception as exc:
            # **Never leave a performed item unreported.** The ServiceTitan
            # write is real and cannot be taken back, but the ledger row that
            # would recognise a redelivery is NOT durable — a Sheets 429 here is
            # routine. Letting this propagate is what made the write duplicate:
            # the lane ended with the item neither ledgered nor reported, the
            # app's lease expired, it redelivered, and we performed it a second
            # time. So report success anyway: the app's own queue then becomes
            # the record that this item is done, and it will not redeliver.
            logger.error(
                "%s outbox item %s (idempotency key %s): the ServiceTitan write SUCCEEDED "
                "(%s) but the ledger flush failed: %s. Reporting success to the app anyway "
                "so the item is not redelivered and re-performed; its queue, not our "
                "ledger, is the record for this one. No further items are performed in "
                "this lane on this run.",
                lane.product,
                item.id,
                item.idempotency_key,
                st_id,
                exc,
            )
            _report(lane, item, succeeded=True, value=st_id)
            # Stop performing: the ledger is not accepting rows, so every further
            # write in this lane would carry the same duplicate risk.
            ledger_unwritable = True
            break
        _report(lane, item, succeeded=True, value=st_id)

    # Safety net only — every recorded item was already flushed above, and
    # OutboxLedger.flush() is a no-op when nothing was ever loaded. Guarded for
    # the same reason as the per-item flush: by here every performed item has
    # already been reported, and raising would turn a completed lane into a
    # `LaneOutcome(error=...)` that hides its counts.
    if not ledger_unwritable:
        try:
            ledger.flush()
        except Exception as exc:
            logger.error(
                "%s outbox: final ledger flush failed: %s. Every item performed this run "
                "was already flushed and reported individually.",
                lane.product,
                exc,
            )
    logger.info(
        "%s outbox drain: claimed=%d succeeded=%d failed=%d replayed=%d",
        lane.product,
        len(items),
        succeeded,
        failed,
        replayed,
    )
    return DrainSummary(claimed=len(items), succeeded=succeeded, failed=failed, replayed=replayed)


def drain_lanes(
    client: ServiceTitanClient,
    lanes: list[OutboxLane],
    ledger: OutboxLedger,
    *,
    limit: int = _DEFAULT_CLAIM_LIMIT,
) -> list[LaneOutcome]:
    """Drain every lane in turn, isolating each from the others.

    **This is the per-lane isolation boundary.** One product's outbox being
    down, slow, 500ing or answering with something that is not the JSON this
    exporter expects ends that lane and nothing else: the remaining lanes still
    drain, and the export feeds — already written to the Sheet before the drain
    starts — are untouched either way.

    Sequential rather than concurrent, deliberately: all lanes share one
    ``_outbox_ledger`` tab through a read-modify-write cycle, and two threads
    flushing it would lose whichever wrote first. The claim limit is small and
    the runner has nothing else to do, so being sequential costs seconds.

    ``Exception``, not a bare ``except``: ``SystemExit`` and
    ``KeyboardInterrupt`` must still end the run.
    """
    outcomes: list[LaneOutcome] = []
    for lane in lanes:
        try:
            outcomes.append(
                LaneOutcome(
                    product=lane.product,
                    summary=drain_outbox(client, lane, ledger, limit=limit),
                )
            )
        except Exception as exc:
            logger.warning(
                "outbox lane %s failed; other lanes and the export feeds are unaffected: %s",
                lane.product,
                exc,
            )
            outcomes.append(LaneOutcome(product=lane.product, error=str(exc)))
    return outcomes

"""Tests for drain_outbox — the claim -> perform -> ledger -> report loop — and
for drain_lanes, the per-lane isolation boundary above it."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from st_exporter.outbox.actions import UnsupportedOutboxKindError
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.drain import DrainSummary, drain_lanes, drain_outbox
from st_exporter.outbox.ledger import OutboxLedger
from st_exporter.sheets import InMemorySheetsStore


def _lane(items, product: str = "traderated", perform=None):
    """A stand-in lane. ``perform`` defaults to returning ``st-<item id>``."""
    lane = MagicMock()
    lane.product = product
    lane.claim.return_value = items
    lane.perform.side_effect = perform or (lambda client, item: f"st-{item.id}")
    return lane


class _RecordingStore(InMemorySheetsStore):
    """InMemorySheetsStore that keeps a snapshot of every grid ever written, so a
    test can assert *when* the ledger was flushed, not just its final content."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[list[list[str]]] = []

    def replace_grid(self, tab_name: str, grid: list[list[str]]) -> None:
        super().replace_grid(tab_name, grid)
        self.writes.append([row[:] for row in grid])


class TestDrainOutbox:
    def test_empty_claim_is_a_full_noop(self) -> None:
        lane = _lane([])
        ledger = OutboxLedger(InMemorySheetsStore())
        summary = drain_outbox(MagicMock(), lane, ledger)
        assert summary == DrainSummary(claimed=0, succeeded=0, failed=0, replayed=0)
        lane.report_success.assert_not_called()
        lane.report_failure.assert_not_called()

    def test_successful_referral_lead_is_recorded_and_reported(self) -> None:
        item = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        lane = _lane([item], perform=lambda client, item: "st-999")
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), lane, ledger)

        assert summary == DrainSummary(claimed=1, succeeded=1, failed=0, replayed=0)
        lane.report_success.assert_called_once_with(item, "st-999")
        assert ledger.get("key-1", "traderated").st_id == "st-999"

    def test_unsupported_kind_is_reported_failed_not_raised(self) -> None:
        item = OutboxItem(id="2", idempotency_key="key-2", kind="mystery", payload={})

        def _raise(client, item):
            raise UnsupportedOutboxKindError("mystery not supported")

        lane = _lane([item], perform=_raise)
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), lane, ledger)

        assert summary == DrainSummary(claimed=1, succeeded=0, failed=1, replayed=0)
        lane.report_failure.assert_called_once_with(item, "mystery not supported")
        assert ledger.get("key-2", "traderated") is None

    def test_one_bad_item_does_not_stop_the_rest_of_the_batch(self) -> None:
        good = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        bad = OutboxItem(id="2", idempotency_key="key-2", kind="referral_lead", payload={})

        def _perform(client, item):
            if item.id == "2":
                raise RuntimeError("network blip")
            return "st-1"

        lane = _lane([bad, good], perform=_perform)
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), lane, ledger)

        assert summary == DrainSummary(claimed=2, succeeded=1, failed=1, replayed=0)

    def test_ledger_is_flushed_after_each_item_not_once_at_the_end(self) -> None:
        """Crash safety: each item's ledger row must be durable before the next
        item is touched, so the store must see a write per performed item — not
        one write covering the whole batch at the end."""
        items = [
            OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={}),
            OutboxItem(id="2", idempotency_key="key-2", kind="referral_lead", payload={}),
        ]
        store = _RecordingStore()
        drain_outbox(MagicMock(), _lane(items), OutboxLedger(store))

        # Snapshot after item 1 must already contain key-1 — i.e. it was written
        # before item 2 was processed, not batched up to the end of the loop.
        assert len(store.writes) >= 2, "expected an incremental flush per performed item"
        keys_at_first_write = {row[0] for row in store.writes[0][1:]}
        assert keys_at_first_write == {"key-1"}
        keys_at_second_write = {row[0] for row in store.writes[1][1:]}
        assert keys_at_second_write == {"key-1", "key-2"}

    def test_report_failure_does_not_lose_ledger_row_or_stop_the_batch(self) -> None:
        """The exact crash-safety bug this ordering fixes: reporting item 1's
        result blows up (the app 5xxs), and item 1 must still stay recorded in
        the ledger *and* item 2 must still be processed."""
        first = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        second = OutboxItem(id="2", idempotency_key="key-2", kind="referral_lead", payload={})
        lane = _lane([first, second])

        def _report(item, st_id):
            if item.id == "1":
                raise httpx.ConnectError("the app is down")

        lane.report_success.side_effect = _report
        store = InMemorySheetsStore()

        summary = drain_outbox(MagicMock(), lane, OutboxLedger(store))

        # The ServiceTitan write happened for both, so both count as succeeded
        # even though item 1's report never landed.
        assert summary == DrainSummary(claimed=2, succeeded=2, failed=0, replayed=0)
        assert [call.args[0].id for call in lane.report_success.call_args_list] == ["1", "2"]

        # Both rows survive in the flushed tab: a next run reloading the ledger
        # replays them instead of re-performing them against ServiceTitan.
        reloaded = OutboxLedger(store)
        assert reloaded.get("key-1", "traderated").st_id == "st-1"
        assert reloaded.get("key-2", "traderated").st_id == "st-2"

    def test_report_failure_on_a_failed_item_does_not_stop_the_batch(self) -> None:
        """Same guard on the failure-reporting path."""
        bad = OutboxItem(id="1", idempotency_key="key-1", kind="mystery", payload={})
        good = OutboxItem(id="2", idempotency_key="key-2", kind="referral_lead", payload={})

        def _perform(client, item):
            if item.kind == "mystery":
                raise UnsupportedOutboxKindError("mystery not supported")
            return "st-2"

        lane = _lane([bad, good], perform=_perform)
        lane.report_failure.side_effect = httpx.ConnectError("down")
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), lane, ledger)

        assert summary == DrainSummary(claimed=2, succeeded=1, failed=1, replayed=0)
        assert ledger.get("key-2", "traderated").st_id == "st-2"

    def test_redelivered_item_is_replayed_not_reperformed(self) -> None:
        """The at-least-once-delivery safety property this whole ledger exists for:
        an item already in the ledger from a prior (possibly crashed) run must be
        re-reported using its recorded st_id, without performing it again."""
        item = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        store = InMemorySheetsStore()

        calls: list[str] = []

        def _perform(client, item):
            calls.append(item.id)
            return "st-999"

        drain_outbox(MagicMock(), _lane([item], perform=_perform), OutboxLedger(store))
        assert calls == ["1"]

        # Simulate the next run: fresh ledger instance reloading the flushed tab.
        lane_2 = _lane([item], perform=_perform)
        summary = drain_outbox(MagicMock(), lane_2, OutboxLedger(store))

        assert calls == ["1"], "a replayed item must not be performed again"
        assert summary == DrainSummary(claimed=1, succeeded=0, failed=0, replayed=1)
        lane_2.report_success.assert_called_once_with(item, "st-999")


class TestProductNamespacing:
    """Two apps minting the same idempotency key must not mask each other.

    Nothing coordinates TradeRated's `referral_lead:<uuid>` with TrueQuote's
    `servicetitan:booking:<sessionId>`, so a collision is a question of when,
    not whether — and in a shared key space the second app's write would be
    skipped forever while being reported succeeded.
    """

    def test_the_same_key_on_two_lanes_is_two_separate_items(self) -> None:
        store = InMemorySheetsStore()
        ledger = OutboxLedger(store)
        shared_key = "collision"

        tr_item = OutboxItem(id="a", idempotency_key=shared_key, kind="referral_lead", payload={})
        tq_item = OutboxItem(id="b", idempotency_key=shared_key, kind="booking", payload={})

        tr_lane = _lane([tr_item], product="traderated", perform=lambda c, i: "lead-1")
        tq_lane = _lane([tq_item], product="truequote", perform=lambda c, i: "booking-1")

        assert drain_outbox(MagicMock(), tr_lane, ledger).succeeded == 1
        tq_summary = drain_outbox(MagicMock(), tq_lane, ledger)

        assert tq_summary == DrainSummary(claimed=1, succeeded=1, failed=0, replayed=0)
        tq_lane.perform.assert_called_once()
        assert ledger.get(shared_key, "traderated").st_id == "lead-1"
        assert ledger.get(shared_key, "truequote").st_id == "booking-1"


class TestDrainLanes:
    def test_every_configured_lane_is_drained(self) -> None:
        lanes = [
            _lane(
                [OutboxItem(id="a", idempotency_key="k-a", kind="referral_lead", payload={})],
                product="traderated",
            ),
            _lane(
                [OutboxItem(id="b", idempotency_key="k-b", kind="booking", payload={})],
                product="truequote",
            ),
        ]
        outcomes = drain_lanes(MagicMock(), lanes, OutboxLedger(InMemorySheetsStore()))

        assert [outcome.product for outcome in outcomes] == ["traderated", "truequote"]
        assert all(outcome.error is None for outcome in outcomes)
        assert all(outcome.summary.succeeded == 1 for outcome in outcomes)

    def test_one_lane_being_unreachable_does_not_stop_the_others(self) -> None:
        """The core per-lane isolation requirement, at the claim call."""
        broken = _lane([], product="traderated")
        broken.claim.side_effect = httpx.ConnectError("refused")
        healthy = _lane(
            [OutboxItem(id="b", idempotency_key="k-b", kind="booking", payload={})],
            product="truequote",
        )

        outcomes = drain_lanes(MagicMock(), [broken, healthy], OutboxLedger(InMemorySheetsStore()))

        assert outcomes[0].product == "traderated"
        assert outcomes[0].summary is None
        assert "refused" in outcomes[0].error
        assert outcomes[1].summary == DrainSummary(claimed=1, succeeded=1, failed=0, replayed=0)
        healthy.report_success.assert_called_once()

    def test_a_lane_returning_garbage_does_not_stop_the_others(self) -> None:
        """Not just "down": a 200 whose body is not the JSON this exporter reads
        raises inside the lane's own client, and must cost only that lane."""
        garbage = _lane([], product="traderated")
        garbage.claim.side_effect = KeyError("idempotency_key")
        healthy = _lane(
            [OutboxItem(id="b", idempotency_key="k-b", kind="booking", payload={})],
            product="truequote",
        )

        outcomes = drain_lanes(MagicMock(), [garbage, healthy], OutboxLedger(InMemorySheetsStore()))

        assert outcomes[0].error is not None
        assert outcomes[1].summary.succeeded == 1

    def test_a_lane_that_500s_on_every_report_still_leaves_the_others_alone(self) -> None:
        noisy = _lane(
            [OutboxItem(id="a", idempotency_key="k-a", kind="referral_lead", payload={})],
            product="traderated",
        )
        noisy.report_success.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        healthy = _lane(
            [OutboxItem(id="b", idempotency_key="k-b", kind="booking", payload={})],
            product="truequote",
        )

        outcomes = drain_lanes(MagicMock(), [noisy, healthy], OutboxLedger(InMemorySheetsStore()))

        # A failed report is swallowed inside the lane's own drain, so the lane
        # still completes — the write happened and the ledger holds it.
        assert outcomes[0].summary == DrainSummary(claimed=1, succeeded=1, failed=0, replayed=0)
        assert outcomes[1].summary.succeeded == 1

    def test_no_lanes_is_an_empty_result_not_an_error(self) -> None:
        assert drain_lanes(MagicMock(), [], OutboxLedger(InMemorySheetsStore())) == []

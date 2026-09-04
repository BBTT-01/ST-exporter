"""Tests for drain_outbox — the claim -> perform -> ledger -> report loop."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from st_exporter.outbox.actions import UnsupportedOutboxKindError
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.drain import DrainSummary, drain_outbox
from st_exporter.outbox.ledger import OutboxLedger
from st_exporter.sheets import InMemorySheetsStore


def _outbox_client(items):
    client = MagicMock()
    client.claim.return_value = items
    return client


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
        outbox_client = _outbox_client([])
        ledger = OutboxLedger(InMemorySheetsStore())
        summary = drain_outbox(MagicMock(), outbox_client, ledger)
        assert summary == DrainSummary(claimed=0, succeeded=0, failed=0, replayed=0)
        outbox_client.report_result.assert_not_called()

    def test_successful_referral_lead_is_recorded_and_reported(self, monkeypatch) -> None:
        item = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        outbox_client = _outbox_client([item])
        monkeypatch.setattr("st_exporter.outbox.drain.perform_item", lambda client, item: "st-999")
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), outbox_client, ledger)

        assert summary == DrainSummary(claimed=1, succeeded=1, failed=0, replayed=0)
        outbox_client.report_result.assert_called_once_with("1", status="succeeded", st_id="st-999")
        assert ledger.get("key-1").st_id == "st-999"

    def test_unsupported_kind_is_reported_failed_not_raised(self, monkeypatch) -> None:
        item = OutboxItem(id="2", idempotency_key="key-2", kind="technician_rating", payload={})
        outbox_client = _outbox_client([item])

        def _raise(client, item):
            raise UnsupportedOutboxKindError("technician_rating not supported")

        monkeypatch.setattr("st_exporter.outbox.drain.perform_item", _raise)
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), outbox_client, ledger)

        assert summary == DrainSummary(claimed=1, succeeded=0, failed=1, replayed=0)
        outbox_client.report_result.assert_called_once_with(
            "2", status="failed", error="technician_rating not supported"
        )
        assert ledger.get("key-2") is None

    def test_one_bad_item_does_not_stop_the_rest_of_the_batch(self, monkeypatch) -> None:
        good = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        bad = OutboxItem(id="2", idempotency_key="key-2", kind="referral_lead", payload={})
        outbox_client = _outbox_client([bad, good])

        def _perform(client, item):
            if item.id == "2":
                raise RuntimeError("network blip")
            return "st-1"

        monkeypatch.setattr("st_exporter.outbox.drain.perform_item", _perform)
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), outbox_client, ledger)

        assert summary == DrainSummary(claimed=2, succeeded=1, failed=1, replayed=0)

    def test_ledger_is_flushed_after_each_item_not_once_at_the_end(self, monkeypatch) -> None:
        """Crash safety: each item's ledger row must be durable before the next
        item is touched, so the store must see a write per performed item — not
        one write covering the whole batch at the end."""
        items = [
            OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={}),
            OutboxItem(id="2", idempotency_key="key-2", kind="referral_lead", payload={}),
        ]
        outbox_client = _outbox_client(items)
        monkeypatch.setattr(
            "st_exporter.outbox.drain.perform_item", lambda client, item: f"st-{item.id}"
        )

        store = _RecordingStore()
        drain_outbox(MagicMock(), outbox_client, OutboxLedger(store))

        # Snapshot after item 1 must already contain key-1 — i.e. it was written
        # before item 2 was processed, not batched up to the end of the loop.
        assert len(store.writes) >= 2, "expected an incremental flush per performed item"
        keys_at_first_write = {row[0] for row in store.writes[0][1:]}
        assert keys_at_first_write == {"key-1"}
        keys_at_second_write = {row[0] for row in store.writes[1][1:]}
        assert keys_at_second_write == {"key-1", "key-2"}

    def test_report_failure_does_not_lose_ledger_row_or_stop_the_batch(self, monkeypatch) -> None:
        """The exact crash-safety bug this ordering fixes: reporting item 1's
        result blows up (a TradeRated 5xx), and item 1 must still stay recorded
        in the ledger *and* item 2 must still be processed."""
        first = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        second = OutboxItem(id="2", idempotency_key="key-2", kind="referral_lead", payload={})
        outbox_client = _outbox_client([first, second])

        def _report(item_id, **kwargs):
            if item_id == "1":
                raise httpx.ConnectError("traderated is down")

        outbox_client.report_result.side_effect = _report
        monkeypatch.setattr(
            "st_exporter.outbox.drain.perform_item", lambda client, item: f"st-{item.id}"
        )
        store = InMemorySheetsStore()

        summary = drain_outbox(MagicMock(), outbox_client, OutboxLedger(store))

        # The ServiceTitan write happened for both, so both count as succeeded
        # even though item 1's report never landed.
        assert summary == DrainSummary(claimed=2, succeeded=2, failed=0, replayed=0)
        assert [call.args[0] for call in outbox_client.report_result.call_args_list] == ["1", "2"]

        # Both rows survive in the flushed tab: a next run reloading the ledger
        # replays them instead of re-performing them against ServiceTitan.
        reloaded = OutboxLedger(store)
        assert reloaded.get("key-1").st_id == "st-1"
        assert reloaded.get("key-2").st_id == "st-2"

    def test_report_failure_on_a_failed_item_does_not_stop_the_batch(self, monkeypatch) -> None:
        """Same guard on the failure-reporting path."""
        bad = OutboxItem(id="1", idempotency_key="key-1", kind="technician_rating", payload={})
        good = OutboxItem(id="2", idempotency_key="key-2", kind="referral_lead", payload={})
        outbox_client = _outbox_client([bad, good])
        outbox_client.report_result.side_effect = [httpx.ConnectError("down"), None]

        def _perform(client, item):
            if item.kind == "technician_rating":
                raise UnsupportedOutboxKindError("technician_rating not supported")
            return "st-2"

        monkeypatch.setattr("st_exporter.outbox.drain.perform_item", _perform)
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), outbox_client, ledger)

        assert summary == DrainSummary(claimed=2, succeeded=1, failed=1, replayed=0)
        assert ledger.get("key-2").st_id == "st-2"

    def test_redelivered_item_is_replayed_not_reperformed(self, monkeypatch) -> None:
        """The at-least-once-delivery safety property this whole ledger exists for:
        an item already in the ledger from a prior (possibly crashed) run must be
        re-reported using its recorded st_id, without calling perform_item again."""
        item = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        outbox_client = _outbox_client([item])
        store = InMemorySheetsStore()
        ledger = OutboxLedger(store)

        calls = []
        monkeypatch.setattr(
            "st_exporter.outbox.drain.perform_item",
            lambda client, item: calls.append(item.id) or "st-999",
        )
        drain_outbox(MagicMock(), outbox_client, ledger)
        assert calls == ["1"]

        # Simulate the next run: fresh ledger instance reloading the flushed tab.
        outbox_client_2 = _outbox_client([item])
        ledger_2 = OutboxLedger(store)
        summary = drain_outbox(MagicMock(), outbox_client_2, ledger_2)

        assert calls == ["1"], "perform_item must not be called again for a replayed item"
        assert summary == DrainSummary(claimed=1, succeeded=0, failed=0, replayed=1)
        outbox_client_2.report_result.assert_called_once_with(
            "1", status="succeeded", st_id="st-999"
        )

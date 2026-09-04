"""Tests for drain_outbox — the claim -> perform -> ledger -> report loop."""

from __future__ import annotations

from unittest.mock import MagicMock

from st_exporter.outbox.actions import UnsupportedOutboxKindError
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.drain import DrainSummary, drain_outbox
from st_exporter.outbox.ledger import OutboxLedger
from st_exporter.sheets import InMemorySheetsStore


def _outbox_client(items):
    client = MagicMock()
    client.claim.return_value = items
    return client


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

"""Tests for OutboxLedger — the _outbox_ledger tab that makes a redelivered
outbox item safe to re-report without re-performing the ServiceTitan write."""

from __future__ import annotations

from st_exporter.outbox.ledger import LedgerEntry, OutboxLedger
from st_exporter.sheets import InMemorySheetsStore


class TestOutboxLedger:
    def test_get_on_empty_store_returns_none(self) -> None:
        ledger = OutboxLedger(InMemorySheetsStore())
        assert ledger.get("missing-key") is None

    def test_record_then_get_round_trips(self) -> None:
        ledger = OutboxLedger(InMemorySheetsStore())
        entry = LedgerEntry(
            idempotency_key="key-1",
            kind="referral_lead",
            st_id="st-1",
            performed_at="2026-09-04T00:00:00+00:00",
        )
        ledger.record(entry)
        assert ledger.get("key-1") == entry

    def test_flush_writes_grid_and_survives_reload(self) -> None:
        store = InMemorySheetsStore()
        ledger = OutboxLedger(store)
        entry = LedgerEntry(
            idempotency_key="key-1",
            kind="referral_lead",
            st_id="st-1",
            performed_at="2026-09-04T00:00:00+00:00",
        )
        ledger.record(entry)
        ledger.flush()

        reloaded = OutboxLedger(store)
        assert reloaded.get("key-1") == entry

    def test_flush_without_any_get_or_record_is_a_noop(self) -> None:
        store = InMemorySheetsStore()
        ledger = OutboxLedger(store)
        ledger.flush()
        assert store.tabs == {}

    def test_malformed_row_is_skipped_not_crashed_on(self) -> None:
        store = InMemorySheetsStore()
        store.replace_grid(
            "_outbox_ledger",
            [["idempotency_key", "kind", "st_id", "performed_at"], ["too-short-row"]],
        )
        ledger = OutboxLedger(store)
        assert ledger.get("too-short-row") is None

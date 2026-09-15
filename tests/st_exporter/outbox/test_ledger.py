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


class TestLegacyRows:
    """The tab shipped with four columns. Those rows are TradeRated's by
    construction — it was the only lane that could write one — and reading them
    as anything else (or skipping them as malformed) would make every
    already-performed referral look new and re-create it in a live CRM."""

    def test_a_four_column_row_is_read_as_traderated(self) -> None:
        store = InMemorySheetsStore()
        store.replace_grid(
            "_outbox_ledger",
            [
                ["idempotency_key", "kind", "st_id", "performed_at"],
                ["key-1", "referral_lead", "st-1", "2026-09-04T00:00:00+00:00"],
            ],
        )
        ledger = OutboxLedger(store)
        assert ledger.get("key-1", "traderated").st_id == "st-1"
        assert ledger.get("key-1") is not None, "the default product must match legacy rows"

    def test_a_blank_product_cell_is_read_as_traderated(self) -> None:
        """Sheets pads short rows out to the widest row on the tab, so the first
        run after this column exists sees `""` rather than a missing cell."""
        store = InMemorySheetsStore()
        store.replace_grid(
            "_outbox_ledger",
            [
                ["idempotency_key", "kind", "st_id", "performed_at", "product"],
                ["key-1", "referral_lead", "st-1", "2026-09-04T00:00:00+00:00", ""],
            ],
        )
        assert OutboxLedger(store).get("key-1", "traderated").st_id == "st-1"

    def test_a_legacy_row_is_rewritten_with_its_product(self) -> None:
        store = InMemorySheetsStore()
        store.replace_grid(
            "_outbox_ledger",
            [
                ["idempotency_key", "kind", "st_id", "performed_at"],
                ["key-1", "referral_lead", "st-1", "2026-09-04T00:00:00+00:00"],
            ],
        )
        ledger = OutboxLedger(store)
        ledger.get("key-1")
        ledger.flush()
        assert store.tabs["_outbox_ledger"][0] == [
            "idempotency_key",
            "kind",
            "st_id",
            "performed_at",
            "product",
        ]
        assert store.tabs["_outbox_ledger"][1][4] == "traderated"


class TestProductNamespace:
    def test_the_same_key_under_two_products_is_two_entries(self) -> None:
        store = InMemorySheetsStore()
        ledger = OutboxLedger(store)
        ledger.record(LedgerEntry("shared", "referral_lead", "lead-1", "t", "traderated"))
        ledger.record(LedgerEntry("shared", "booking", "booking-1", "t", "truequote"))
        ledger.flush()

        reloaded = OutboxLedger(store)
        assert reloaded.get("shared", "traderated").st_id == "lead-1"
        assert reloaded.get("shared", "truequote").st_id == "booking-1"
        assert reloaded.get("shared", "profitwizard") is None

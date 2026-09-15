"""Tests for ImageLedger — the only place a repeat upload can be avoided.

TrueQuote's endpoint is POST-only with no probe, so this tab is what stops the
same bytes crossing the wire on every scheduled run.
"""

from __future__ import annotations

from st_exporter.images.ledger import ImageLedger, ImageLedgerEntry
from st_exporter.sheets import InMemorySheetsStore

ENTRY = ImageLedgerEntry(
    idempotency_key="key-1",
    asset_ref="100:a1",
    storage_path="co/servicetitan/assets/deadbeef.png",
    uploaded_at="2026-09-14T12:00:00+00:00",
)


class TestImageLedger:
    def test_empty_store_knows_nothing(self) -> None:
        assert not ImageLedger(InMemorySheetsStore()).has("key-1")

    def test_record_then_has_round_trips(self) -> None:
        ledger = ImageLedger(InMemorySheetsStore())
        ledger.record(ENTRY)
        assert ledger.has("key-1")

    def test_flush_survives_a_reload(self) -> None:
        store = InMemorySheetsStore()
        ledger = ImageLedger(store)
        ledger.record(ENTRY)
        ledger.flush()
        assert ImageLedger(store).has("key-1")

    def test_flush_is_a_noop_before_anything_touched_it(self) -> None:
        store = InMemorySheetsStore()
        ImageLedger(store).flush()
        assert store.tabs == {}

    def test_a_malformed_row_is_skipped_not_fatal(self) -> None:
        store = InMemorySheetsStore()
        store.replace_grid(
            "_image_ledger",
            [["idempotency_key", "asset_ref", "storage_path", "uploaded_at"], ["short-row"]],
        )
        ledger = ImageLedger(store)
        assert not ledger.has("short-row")

    def test_keep_prunes_assets_the_catalogue_no_longer_has(self) -> None:
        store = InMemorySheetsStore()
        ledger = ImageLedger(store)
        ledger.record(ENTRY)
        ledger.record(
            ImageLedgerEntry(
                idempotency_key="key-2", asset_ref="101:a2", storage_path="p", uploaded_at="t"
            )
        )
        ledger.keep({"key-1"})
        ledger.flush()

        reloaded = ImageLedger(store)
        assert reloaded.has("key-1")
        assert not reloaded.has("key-2")

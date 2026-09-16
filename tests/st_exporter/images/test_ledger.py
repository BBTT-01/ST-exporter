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
    verified_at="2026-09-14T12:00:00+00:00",
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
            [["idempotency_key", "asset_ref", "storage_path", "verified_at"], ["short-row"]],
        )
        ledger = ImageLedger(store)
        assert not ledger.has("short-row")

    def test_an_older_four_column_ledger_loads_unchanged(self) -> None:
        """`verified_at` renamed the column `uploaded_at` used to be, in place.

        A rename that shifted the column would read every live ledger as
        malformed and re-upload every image in the catalogue once.
        """
        store = InMemorySheetsStore()
        store.replace_grid(
            "_image_ledger",
            [
                ["idempotency_key", "asset_ref", "storage_path", "uploaded_at"],
                ["key-1", "100:a1", "co/st/assets/x.png", "2026-09-14T12:00:00+00:00"],
            ],
        )
        ledger = ImageLedger(store)
        assert ledger.has("key-1")
        assert ledger.last_verified("100:a1") == "2026-09-14T12:00:00+00:00"

    def test_an_asset_the_ledger_never_saw_has_no_verification(self) -> None:
        assert ImageLedger(InMemorySheetsStore()).last_verified("100:a1") is None

    def test_verify_moves_an_entry_to_the_back_of_the_queue(self) -> None:
        """A re-checked asset must not sort first on every later pass.

        Leave the original timestamp and the same prefix of the catalogue is
        re-downloaded every run while everything behind it is never reached —
        the failure the ordering exists to prevent.
        """
        ledger = ImageLedger(InMemorySheetsStore())
        ledger.record(ENTRY)
        ledger.verify("key-1", "2026-09-15T09:00:00+00:00")
        assert ledger.last_verified("100:a1") == "2026-09-15T09:00:00+00:00"

    def test_verifying_a_key_the_ledger_does_not_hold_is_a_noop(self) -> None:
        ledger = ImageLedger(InMemorySheetsStore())
        ledger.verify("key-1", "2026-09-15T09:00:00+00:00")
        assert not ledger.has("key-1")

    def test_a_refreshed_timestamp_survives_a_flush(self) -> None:
        store = InMemorySheetsStore()
        ledger = ImageLedger(store)
        ledger.record(ENTRY)
        ledger.verify("key-1", "2026-09-15T09:00:00+00:00")
        ledger.flush()
        assert ImageLedger(store).last_verified("100:a1") == "2026-09-15T09:00:00+00:00"

    def test_the_latest_of_an_assets_several_keys_is_the_answer(self) -> None:
        """One asset holds a key per content hash until `keep` prunes the old
        ones. "When did we last look at this" is the most recent of them."""
        ledger = ImageLedger(InMemorySheetsStore())
        ledger.record(ENTRY)
        ledger.record(
            ImageLedgerEntry(
                idempotency_key="key-2",
                asset_ref="100:a1",
                storage_path="p",
                verified_at="2026-09-15T09:00:00+00:00",
            )
        )
        assert ledger.last_verified("100:a1") == "2026-09-15T09:00:00+00:00"

    def test_keep_forgets_the_verification_of_a_pruned_asset(self) -> None:
        ledger = ImageLedger(InMemorySheetsStore())
        ledger.record(ENTRY)
        ledger.keep(set())
        assert ledger.last_verified("100:a1") is None

    def test_keep_prunes_assets_the_catalogue_no_longer_has(self) -> None:
        store = InMemorySheetsStore()
        ledger = ImageLedger(store)
        ledger.record(ENTRY)
        ledger.record(
            ImageLedgerEntry(
                idempotency_key="key-2", asset_ref="101:a2", storage_path="p", verified_at="t"
            )
        )
        ledger.keep({"key-1"})
        ledger.flush()

        reloaded = ImageLedger(store)
        assert reloaded.has("key-1")
        assert not reloaded.has("key-2")

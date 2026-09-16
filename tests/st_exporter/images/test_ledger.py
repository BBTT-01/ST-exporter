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
                idempotency_key="key-2", asset_ref="101:a2", storage_path="p", verified_at="t"
            )
        )
        ledger.keep({"key-1"})
        ledger.flush()

        reloaded = ImageLedger(store)
        assert reloaded.has("key-1")
        assert not reloaded.has("key-2")


def test_a_ledger_written_by_an_older_exporter_loads_unchanged() -> None:
    """`uploaded_at` was renamed to `verified_at` IN PLACE — same column, same
    position, same data. A live tenant's existing ledger must keep working, or
    the rename costs a full re-upload of every image it holds."""
    store = InMemorySheetsStore()
    store.replace_grid(
        "_image_ledger",
        [
            ["idempotency_key", "asset_ref", "storage_path", "uploaded_at"],
            ["key-1", "100:a1", "co/st/x.png", "2026-09-14T12:00:00+00:00"],
        ],
    )

    ledger = ImageLedger(store)

    assert ledger.has("key-1")
    assert ledger.last_verified("100:a1") == "2026-09-14T12:00:00+00:00"


def test_an_asset_never_seen_has_no_verification_time() -> None:
    """None, not "", because None is what sorts an unseen asset to the FRONT of
    the next pass — the whole mechanism by which a bounded run makes forward
    progress instead of re-treading its prefix."""
    assert ImageLedger(InMemorySheetsStore()).last_verified("999:nope") is None


def test_verify_moves_an_asset_to_the_back_of_the_next_pass() -> None:
    """Re-confirming known bytes must refresh the timestamp.

    Without this an already-delivered asset keeps its original time for ever,
    sorts first on every later run, is re-examined every time, and the assets
    behind it are never reached — which is the shape run 35130164187 was stuck
    in.
    """
    store = InMemorySheetsStore()
    ledger = ImageLedger(store)
    ledger.record(ENTRY)
    assert ledger.last_verified(ENTRY.asset_ref) == ENTRY.verified_at

    ledger.verify(ENTRY.idempotency_key, "2026-09-15T12:00:00+00:00")

    assert ledger.last_verified(ENTRY.asset_ref) == "2026-09-15T12:00:00+00:00"
    ledger.flush()
    assert store.tabs["_image_ledger"][1][3] == "2026-09-15T12:00:00+00:00"


def test_verifying_an_unknown_key_is_a_no_op() -> None:
    """This file must never be the reason a run fails."""
    ledger = ImageLedger(InMemorySheetsStore())
    ledger.verify("never-heard-of-it", "2026-09-15T12:00:00+00:00")
    assert ledger.last_verified("100:a1") is None


def test_keys_for_answers_every_key_recorded_for_one_asset() -> None:
    """How a pass that skipped an asset WITHOUT downloading it still says "this
    is still here" — a key absent from `seen_keys` is what `keep` prunes."""
    ledger = ImageLedger(InMemorySheetsStore())
    ledger.record(ENTRY)
    ledger.record(
        ImageLedgerEntry(
            idempotency_key="key-old",
            asset_ref=ENTRY.asset_ref,
            storage_path="co/st/old.png",
            verified_at="2026-09-01T00:00:00+00:00",
        )
    )

    assert ledger.keys_for(ENTRY.asset_ref) == {ENTRY.idempotency_key, "key-old"}
    # The LATEST of an asset's keys is what orders the pass.
    assert ledger.last_verified(ENTRY.asset_ref) == ENTRY.verified_at
    assert ledger.keys_for("nothing-here") == set()

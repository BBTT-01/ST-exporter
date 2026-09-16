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


class TestCacheValidators:
    """The ledger is where an `ETag` survives between runs, so a weekly
    re-verification can be a 304 instead of a re-download."""

    def test_validators_round_trip_through_a_flush(self) -> None:
        store = InMemorySheetsStore()
        ledger = ImageLedger(store)
        ledger.record(
            ImageLedgerEntry(
                idempotency_key="key-1",
                asset_ref="100:a1",
                storage_path="p",
                verified_at="2026-09-14T12:00:00+00:00",
                etag='"v1"',
                last_modified="Mon, 14 Sep 2026 00:00:00 GMT",
            )
        )
        ledger.flush()

        assert ImageLedger(store).validators_for("100:a1") == (
            '"v1"',
            "Mon, 14 Sep 2026 00:00:00 GMT",
        )

    def test_an_asset_with_no_entry_offers_nothing_to_quote_back(self) -> None:
        assert ImageLedger(InMemorySheetsStore()).validators_for("100:a1") == ("", "")

    def test_the_newest_entry_wins(self) -> None:
        """One asset can hold several keys — a replaced image leaves its
        predecessor behind until `keep` prunes it — and only the newest
        describes the bytes TrueQuote currently holds."""
        ledger = ImageLedger(InMemorySheetsStore())
        ledger.record(
            ImageLedgerEntry("old", "100:a1", "p", "2026-09-01T00:00:00+00:00", '"old"', "")
        )
        ledger.record(
            ImageLedgerEntry("new", "100:a1", "p", "2026-09-14T00:00:00+00:00", '"new"', "")
        )
        assert ledger.validators_for("100:a1")[0] == '"new"'

    def test_a_four_column_row_still_loads(self) -> None:
        """Every ledger written before this feature is four columns wide.
        Rejecting it as malformed would forget a whole catalogue's uploads and
        re-send every byte on the upgrade run."""
        store = InMemorySheetsStore()
        store.replace_grid(
            "_image_ledger",
            [
                ["idempotency_key", "asset_ref", "storage_path", "verified_at"],
                ["key-1", "100:a1", "p", "2026-09-14T12:00:00+00:00"],
            ],
        )
        ledger = ImageLedger(store)
        assert ledger.has("key-1")
        assert ledger.last_verified("100:a1") == "2026-09-14T12:00:00+00:00"
        assert ledger.validators_for("100:a1") == ("", "")

    def test_verify_ref_restamps_every_key_and_stores_the_validator(self) -> None:
        """A 304 carries no body, so no content hash, so no key — the asset
        reference is the only handle the pass has."""
        ledger = ImageLedger(InMemorySheetsStore())
        ledger.record(ImageLedgerEntry("a", "100:a1", "p", "2026-01-01T00:00:00+00:00"))
        ledger.record(ImageLedgerEntry("b", "100:a1", "p", "2026-01-02T00:00:00+00:00"))

        ledger.verify_ref("100:a1", "2026-09-14T12:00:00+00:00", etag='"v2"')

        assert ledger.last_verified("100:a1") == "2026-09-14T12:00:00+00:00"
        assert ledger.validators_for("100:a1") == ('"v2"', "")

    def test_a_re_check_with_no_new_validator_keeps_the_old_one(self) -> None:
        """Erasing a validator because one response omitted it would silently
        put that asset back on the full-re-download schedule."""
        ledger = ImageLedger(InMemorySheetsStore())
        ledger.record(ImageLedgerEntry("a", "100:a1", "p", "2026-01-01T00:00:00+00:00", '"v1"', ""))
        ledger.verify("a", "2026-09-14T12:00:00+00:00")
        assert ledger.validators_for("100:a1") == ('"v1"', "")


class TestARememberedRejection:
    """A permanent refusal (TrueQuote 413/422) is a fact about these exact
    bytes, and the only place it can be remembered is here. Without it the pass
    re-downloads and re-POSTs a refused image on every run for ever.
    """

    def test_a_rejection_round_trips_through_a_flush(self) -> None:
        store = InMemorySheetsStore()
        ledger = ImageLedger(store)
        ledger.record_rejected(ENTRY)
        ledger.flush()

        reloaded = ImageLedger(store)
        assert reloaded.has("key-1")
        assert reloaded.is_rejected("key-1") is True

    def test_a_delivered_entry_is_not_a_rejection(self) -> None:
        ledger = ImageLedger(InMemorySheetsStore())
        ledger.record(ENTRY)
        assert ledger.has("key-1")
        assert ledger.is_rejected("key-1") is False
        assert ImageLedger(InMemorySheetsStore()).is_rejected("never-seen") is False

    def test_keep_does_not_drop_a_rejection_the_run_still_sees(self) -> None:
        store = InMemorySheetsStore()
        ledger = ImageLedger(store)
        ledger.record_rejected(ENTRY)
        ledger.keep({"key-1"})
        ledger.flush()

        reloaded = ImageLedger(store)
        assert reloaded.is_rejected("key-1") is True

    def test_re_verifying_a_rejection_keeps_the_marker(self) -> None:
        """The pass re-stamps a remembered rejection so it sorts to the BACK of
        the next pass. That must not quietly turn it into a delivery."""
        ledger = ImageLedger(InMemorySheetsStore())
        ledger.record_rejected(ENTRY)
        ledger.verify("key-1", "2026-09-21T12:00:00+00:00")
        assert ledger.is_rejected("key-1") is True
        assert ledger.last_verified("100:a1") == "2026-09-21T12:00:00+00:00"

    def test_a_six_column_ledger_written_before_this_feature_still_loads(self) -> None:
        """The upgrade run must not mistake every existing row for a rejection,
        or re-upload a whole catalogue: a blank seventh column is "delivered"."""
        store = InMemorySheetsStore()
        store.replace_grid(
            "_image_ledger",
            [
                [
                    "idempotency_key",
                    "asset_ref",
                    "storage_path",
                    "verified_at",
                    "etag",
                    "last_modified",
                ],
                ["key-1", "100:a1", "p", "2026-09-14T12:00:00+00:00", '"e"', "Mon, 14 Sep 2026"],
            ],
        )
        ledger = ImageLedger(store)
        assert ledger.has("key-1")
        assert ledger.is_rejected("key-1") is False
        assert ledger.validators_for("100:a1") == ('"e"', "Mon, 14 Sep 2026")

    def test_a_four_column_ledger_still_loads_and_is_not_a_rejection(self) -> None:
        store = InMemorySheetsStore()
        store.replace_grid(
            "_image_ledger",
            [
                ["idempotency_key", "asset_ref", "storage_path", "uploaded_at"],
                ["key-1", "100:a1", "p", "2026-09-14T12:00:00+00:00"],
            ],
        )
        ledger = ImageLedger(store)
        assert ledger.has("key-1")
        assert ledger.is_rejected("key-1") is False

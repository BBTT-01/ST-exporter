"""Crash-safe idempotency ledger for the Outbox drain.

Every outbox lane delivers at-least-once (TradeRated's spec.md, "Outbox
contract"; TrueQuote's `booking-outbox.ts` replay fence): a run that crashes
after writing to ServiceTitan but before reporting the result will see the same
item claimed again next run. This ledger — a tab on the private raw-cache Sheet,
never the shared Export Store — records every idempotency_key this exporter has
already performed, so a redelivered item is recognised and only re-reported,
never re-written to ServiceTitan. This is what satisfies the spec's "yours needs
to be safe to run twice" requirement; it does not depend on ServiceTitan itself
having any idempotency concept, because it doesn't.

**Keys are namespaced by product.** Three apps mint their own idempotency keys
with no coordination — TradeRated's are `referral_lead:<uuid>`, TrueQuote's are
`servicetitan:booking:<sessionId>` — and nothing stops two of them colliding.
A collision in a shared key space would make one app's item look already-
performed because the *other* app had performed something, and the write would
be silently skipped forever. So the lookup key is the pair
``(product, idempotency_key)``.

**Legacy rows.** The tab shipped with four columns and no product. Those rows
are TradeRated's by construction (it was the only lane), so a short row is read
as ``product="traderated"`` rather than skipped — skipping them would make every
already-performed referral look new and re-create it in the contractor's CRM the
first time a contractor upgrades past this version.
"""

from __future__ import annotations

from dataclasses import dataclass

from st_exporter.sheets import SheetsPort

_TAB_NAME = "_outbox_ledger"
# `product` is last so the column order of the original four is untouched: a
# human (or a script) reading an existing `_outbox_ledger` tab sees the same
# columns in the same places, with one appended.
_COLUMNS = ("idempotency_key", "kind", "st_id", "performed_at", "product")
# The width the tab shipped with. A row at least this wide is readable; a row
# narrower than this is genuinely corrupt.
_LEGACY_WIDTH = 4
# What a row with no product column means. Not a general default — it is a
# statement about history: before this column existed, TradeRated was the only
# lane that could write a row.
LEGACY_PRODUCT = "traderated"


@dataclass(frozen=True)
class LedgerEntry:
    idempotency_key: str
    kind: str
    st_id: str
    performed_at: str
    # Defaulted so the three-year-old four-field construction still compiles,
    # and so a caller that forgets it lands on the same lane the legacy rows
    # belong to rather than on a silently separate key space.
    product: str = LEGACY_PRODUCT


class OutboxLedger:
    """Read-modify-write wrapper around the `_outbox_ledger` tab.

    Loads lazily on first `.get()`/`.record()` (not in `__init__`) so
    constructing one never issues a Sheets read by itself; `.flush()` is a
    no-op until something actually touched the ledger, so a drain that claims
    zero items makes zero Sheets calls.

    One ledger instance is shared by every lane in a run: they all read and
    write the same tab, and a single read-modify-write cycle over the whole tab
    is what keeps lane B from clobbering lane A's rows on flush.

    **Because it is shared, "this ledger is not accepting rows" is shared too.**
    A failed flush is a fact about the Sheet, not about the lane that happened to
    hit it, so it is recorded HERE (:meth:`mark_unwritable`) rather than in a
    local variable inside one drain. When it lived in a local, each later lane
    performed exactly one unledgered ServiceTitan write before hitting the same
    failing flush — three lanes, three real writes at risk of duplicating.
    """

    def __init__(self, store: SheetsPort) -> None:
        self._store = store
        self._entries: dict[tuple[str, str], LedgerEntry] = {}
        self._loaded = False
        self._unwritable = False

    @property
    def unwritable(self) -> bool:
        """True once a flush has failed: no further ServiceTitan write is safe.

        Not reset within a run. The next run re-reads the tab and starts clean.
        """
        return self._unwritable

    def mark_unwritable(self) -> None:
        """Record that this ledger could not be flushed, for EVERY lane sharing it."""
        self._unwritable = True

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        grid = self._store.read_grid(_TAB_NAME)
        for row in grid[1:]:  # skip header
            if len(row) < _LEGACY_WIDTH:
                continue  # malformed row; never crash a run over ledger corruption
            key, kind, st_id, performed_at = row[:_LEGACY_WIDTH]
            # A blank cell is as absent as a missing one: Sheets pads a short
            # row out to the widest row on the tab, so the first run after this
            # column is added reads `""` for every legacy row, not a short row.
            product = (row[_LEGACY_WIDTH] if len(row) > _LEGACY_WIDTH else "") or LEGACY_PRODUCT
            entry = LedgerEntry(key, kind, st_id, performed_at, product)
            self._entries[(entry.product, entry.idempotency_key)] = entry
        self._loaded = True

    def get(self, idempotency_key: str, product: str = LEGACY_PRODUCT) -> LedgerEntry | None:
        self._ensure_loaded()
        return self._entries.get((product, idempotency_key))

    def record(self, entry: LedgerEntry) -> None:
        self._ensure_loaded()
        self._entries[(entry.product, entry.idempotency_key)] = entry

    def flush(self) -> None:
        """Write the full ledger back in one call. No-op if nothing was loaded."""
        if not self._loaded:
            return
        grid: list[list[str]] = [list(_COLUMNS)]
        grid.extend(
            [e.idempotency_key, e.kind, e.st_id, e.performed_at, e.product]
            for e in self._entries.values()
        )
        self._store.replace_grid(_TAB_NAME, grid)

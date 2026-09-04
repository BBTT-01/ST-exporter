"""Crash-safe idempotency ledger for the Outbox drain.

TradeRated's claim endpoint delivers at-least-once (spec.md's "Outbox contract"):
a run that crashes after writing to ServiceTitan but before reporting the result
will see the same item claimed again next run. This ledger — a tab on the
private raw-cache Sheet, never the shared Export Store — records every
idempotency_key this exporter has already performed, so a redelivered item is
recognised and only re-reported, never re-written to ServiceTitan. This is what
satisfies the spec's "yours needs to be safe to run twice" requirement; it does
not depend on ServiceTitan itself having any idempotency concept, because it
doesn't.
"""

from __future__ import annotations

from dataclasses import dataclass

from st_exporter.sheets import SheetsPort

_TAB_NAME = "_outbox_ledger"
_COLUMNS = ("idempotency_key", "kind", "st_id", "performed_at")


@dataclass(frozen=True)
class LedgerEntry:
    idempotency_key: str
    kind: str
    st_id: str
    performed_at: str


class OutboxLedger:
    """Read-modify-write wrapper around the `_outbox_ledger` tab.

    Loads lazily on first `.get()`/`.record()` (not in `__init__`) so
    constructing one never issues a Sheets read by itself; `.flush()` is a
    no-op until something actually touched the ledger, so a drain that claims
    zero items makes zero Sheets calls.
    """

    def __init__(self, store: SheetsPort) -> None:
        self._store = store
        self._entries: dict[str, LedgerEntry] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        grid = self._store.read_grid(_TAB_NAME)
        for row in grid[1:]:  # skip header
            if len(row) < len(_COLUMNS):
                continue  # malformed row; never crash a run over ledger corruption
            entry = LedgerEntry(*row[: len(_COLUMNS)])
            self._entries[entry.idempotency_key] = entry
        self._loaded = True

    def get(self, idempotency_key: str) -> LedgerEntry | None:
        self._ensure_loaded()
        return self._entries.get(idempotency_key)

    def record(self, entry: LedgerEntry) -> None:
        self._ensure_loaded()
        self._entries[entry.idempotency_key] = entry

    def flush(self) -> None:
        """Write the full ledger back in one call. No-op if nothing was loaded."""
        if not self._loaded:
            return
        grid: list[list[str]] = [list(_COLUMNS)]
        grid.extend(
            [e.idempotency_key, e.kind, e.st_id, e.performed_at] for e in self._entries.values()
        )
        self._store.replace_grid(_TAB_NAME, grid)

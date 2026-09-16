"""Record of which image bytes this exporter has already delivered.

TrueQuote's endpoint offers no cheap way to ask "do you already have this?" —
``/pricebook-image`` is POST-only, with no GET, no HEAD and no manifest, and its
200 body reports whether a *row* existed, not whether the bytes match. So the
only place a repeat upload can be avoided before it happens is here, on the
runner.

Same shape and same private home as ``outbox/ledger.py``: a tab on the raw-cache
Sheet the exporter's own service account owns, never the shared Export Store. A
lost or corrupted ledger costs bandwidth on the next run and nothing else —
re-POSTing is safe by construction (see ``client.py``), so this file must never
be the reason a run fails.

``verified_at`` is also what ORDERS the next pass. A run works through the
catalogue oldest-verification-first, so an asset this ledger has never heard of
is attempted before one it confirmed an hour ago. That is what lets a pass with
a time budget sweep a 7,000-image catalogue over several runs instead of
re-treading the same prefix until the runner kills it — see ``upload.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from st_exporter.sheets import SheetsPort

_TAB_NAME = "_image_ledger"
# `verified_at` occupies the column an older exporter wrote `uploaded_at` into,
# positionally identical, so an existing ledger loads unchanged. The name is the
# honest one: the value is refreshed every time a later run re-downloads the same
# bytes and finds them already delivered, not only when bytes are sent.
_COLUMNS = ("idempotency_key", "asset_ref", "storage_path", "verified_at")


@dataclass(frozen=True)
class ImageLedgerEntry:
    idempotency_key: str
    asset_ref: str
    storage_path: str
    verified_at: str


class ImageLedger:
    """Read-modify-write wrapper around the `_image_ledger` tab.

    Loads lazily, so constructing one issues no Sheets read and ``flush()`` is a
    no-op until something touched it. "Touched" includes ``keep()`` and every
    read — ``has()`` loads, and so does the ``keep()`` that prunes — so a
    pricebook run that reaches the image pass at all will read and rewrite this
    tab even when it uploads nothing. Only a run with NO image pass (no image
    client, or a dry run) makes zero Sheets calls here.
    """

    def __init__(self, store: SheetsPort) -> None:
        self._store = store
        self._entries: dict[str, ImageLedgerEntry] = {}
        self._by_ref: dict[str, str] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        grid = self._store.read_grid(_TAB_NAME)
        for row in grid[1:]:  # skip header
            if len(row) < len(_COLUMNS):
                continue  # malformed row; never crash a run over ledger corruption
            self._remember(ImageLedgerEntry(*row[: len(_COLUMNS)]))
        self._loaded = True

    def _remember(self, entry: ImageLedgerEntry) -> None:
        self._entries[entry.idempotency_key] = entry
        previous = self._by_ref.get(entry.asset_ref, "")
        self._by_ref[entry.asset_ref] = max(previous, entry.verified_at)

    def has(self, idempotency_key: str) -> bool:
        self._ensure_loaded()
        return idempotency_key in self._entries

    def last_verified(self, asset_ref: str) -> str | None:
        """When this asset was last confirmed delivered, or None if never.

        One asset can hold several keys — the key carries a content hash, so a
        replaced image leaves its predecessor behind until ``keep`` prunes it.
        The latest of them is the answer: "when did we last look at this", which
        is what the pass orders by.
        """
        self._ensure_loaded()
        return self._by_ref.get(asset_ref)

    def record(self, entry: ImageLedgerEntry) -> None:
        self._ensure_loaded()
        self._remember(entry)

    def verify(self, idempotency_key: str, now: str) -> None:
        """Note that these exact bytes were re-checked and are still delivered.

        Without this an already-uploaded asset would keep its original timestamp
        forever, sort to the front of every later pass, and be re-downloaded on
        every run while the assets behind it were never reached.
        """
        self._ensure_loaded()
        entry = self._entries.get(idempotency_key)
        if entry is None:
            return
        self._remember(replace(entry, verified_at=now))

    def keep(self, idempotency_keys: set[str]) -> None:
        """Drop entries for assets this run no longer sees.

        Without this the ledger grows forever: every image that was ever
        replaced leaves its old content hash behind. Only called when a run has
        actually enumerated the whole catalogue, so "not seen" really means
        "gone", not "not looked at".
        """
        self._ensure_loaded()
        kept = {k: v for k, v in self._entries.items() if k in idempotency_keys}
        self._entries = {}
        self._by_ref = {}
        for entry in kept.values():
            self._remember(entry)

    def flush(self) -> None:
        """Write the full ledger back in one call. No-op if nothing was loaded."""
        if not self._loaded:
            return
        grid: list[list[str]] = [list(_COLUMNS)]
        grid.extend(
            [e.idempotency_key, e.asset_ref, e.storage_path, e.verified_at]
            for e in self._entries.values()
        )
        self._store.replace_grid(_TAB_NAME, grid)

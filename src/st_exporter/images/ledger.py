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
# honest one: the value is refreshed every time a later run confirms the same
# bytes are still delivered, not only when bytes are sent.
#
# `etag` / `last_modified` are the HTTP cache validators the SERVER gave us for
# these bytes, quoted back on the next check as `If-None-Match` /
# `If-Modified-Since` so an unchanged asset answers 304 and costs no bytes. Both
# blank is the ordinary, supported state — it means that server offered no
# validator (or that this row predates the columns), and the pass then falls
# back to downloading the asset outright, which is what it always did.
_COLUMNS = (
    "idempotency_key",
    "asset_ref",
    "storage_path",
    "verified_at",
    "etag",
    "last_modified",
    # A PERMANENT refusal by TrueQuote for these exact bytes (`_RETRYABLE_STATUSES`
    # in `client.py` says which are not permanent). Written so the next run does
    # not re-download and re-POST an image that will be refused again, for ever.
    # Seventh column, so every ledger written before it pads to blank = "not
    # rejected" and loads unchanged.
    "rejected",
)
# How many leading columns a row MUST have to be usable. Every ledger written
# before conditional requests existed has exactly these four and nothing else;
# requiring all six would silently discard the whole existing ledger on the
# upgrade run and re-upload a 7,000-image catalogue from scratch.
_REQUIRED_COLUMNS = 4

# What the `rejected` column holds when the entry is a remembered refusal.
# Anything else — blank, most of all — means an ordinary delivered row.
_REJECTED_MARKER = "rejected"


@dataclass(frozen=True)
class ImageLedgerEntry:
    idempotency_key: str
    asset_ref: str
    storage_path: str
    verified_at: str
    # The server's own validators for these exact bytes. Blank when it offered
    # none — never invented, because a fabricated `If-None-Match` would get a
    # 200 at best and a wrong 304 at worst.
    etag: str = ""
    last_modified: str = ""
    # True when this row remembers a PERMANENT rejection rather than a
    # delivery: the bytes reached TrueQuote and TrueQuote refused them. The key
    # still hashes the payload, so replaced bytes get a new key and are tried
    # again; only the identical image is spared the round trip.
    rejected: bool = False


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
        # asset_ref -> the latest `verified_at` any of its keys carries. Kept
        # beside `_entries` rather than derived on demand because the pass reads
        # it once per asset while ordering the catalogue.
        self._by_ref: dict[str, str] = {}
        # asset_ref -> every key recorded for it. An asset the pass skips
        # WITHOUT downloading has no content hash, so it cannot contribute its
        # key to ``seen_keys`` by the usual door — and a key missing from
        # ``seen_keys`` is exactly what ``keep`` prunes. This is how a cheap skip
        # says "this asset is still in the catalogue" without opening it.
        self._keys_by_ref: dict[str, set[str]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        grid = self._store.read_grid(_TAB_NAME)
        for row in grid[1:]:  # skip header
            if len(row) < _REQUIRED_COLUMNS:
                continue  # malformed row; never crash a run over ledger corruption
            # Pad rather than reject: a four-column row is a PRE-VALIDATOR row,
            # not a corrupt one, and it must keep its `verified_at` — that is
            # what stops the upgrade run re-downloading the whole catalogue.
            padded = list(row[: len(_COLUMNS)])
            padded += [""] * (len(_COLUMNS) - len(padded))
            self._remember(
                ImageLedgerEntry(
                    idempotency_key=padded[0],
                    asset_ref=padded[1],
                    storage_path=padded[2],
                    verified_at=padded[3],
                    etag=padded[4],
                    last_modified=padded[5],
                    rejected=padded[6] == _REJECTED_MARKER,
                )
            )
        self._loaded = True

    def _remember(self, entry: ImageLedgerEntry) -> None:
        self._entries[entry.idempotency_key] = entry
        previous = self._by_ref.get(entry.asset_ref, "")
        self._by_ref[entry.asset_ref] = max(previous, entry.verified_at)
        self._keys_by_ref.setdefault(entry.asset_ref, set()).add(entry.idempotency_key)

    def has(self, idempotency_key: str) -> bool:
        self._ensure_loaded()
        return idempotency_key in self._entries

    def is_rejected(self, idempotency_key: str) -> bool:
        """True when this ledger remembers TrueQuote PERMANENTLY refusing these bytes.

        The other half of ``has``: both say "do not POST this", for opposite
        reasons, and the pass counts them apart because they mean opposite
        things about the catalogue.
        """
        self._ensure_loaded()
        entry = self._entries.get(idempotency_key)
        return entry is not None and entry.rejected

    def last_verified(self, asset_ref: str) -> str | None:
        """When this asset was last confirmed delivered, or None if never.

        One asset can hold several keys — the key carries a content hash, so a
        replaced image leaves its predecessor behind until ``keep`` prunes it.
        The latest of them is the answer to "when did we last look at this",
        which is what the pass orders by and what lets it skip a re-download.

        ISO-8601 UTC strings throughout, so a lexical ``max`` is a chronological
        one. Every writer is ``run_at``/``now`` from ``run.py``, which is
        ``datetime.now(timezone.utc).isoformat()``.
        """
        self._ensure_loaded()
        return self._by_ref.get(asset_ref)

    def validators_for(self, asset_ref: str) -> tuple[str, str]:
        """``(etag, last_modified)`` from this asset's most recent entry.

        Empty strings mean "nothing to quote back" — either the server never
        offered a validator or this row predates the columns — and the caller
        then issues a plain, unconditional GET. Picking the most recently
        verified entry matters because one asset can hold several keys (a
        replaced image leaves its predecessor behind until ``keep`` prunes it)
        and only the newest one describes the bytes TrueQuote currently holds.
        """
        self._ensure_loaded()
        newest: ImageLedgerEntry | None = None
        for key in self._keys_by_ref.get(asset_ref, ()):
            entry = self._entries.get(key)
            if entry is None:
                continue
            if newest is None or entry.verified_at > newest.verified_at:
                newest = entry
        if newest is None:
            return ("", "")
        return (newest.etag, newest.last_modified)

    def keys_for(self, asset_ref: str) -> set[str]:
        """Every key this ledger holds for one asset.

        Handed straight to ``seen_keys`` by a pass that skipped the asset
        without downloading it: the asset is demonstrably still in the
        catalogue, so its entries must survive the prune even though no content
        hash was computed this run.
        """
        self._ensure_loaded()
        return set(self._keys_by_ref.get(asset_ref, ()))

    def record(self, entry: ImageLedgerEntry) -> None:
        self._ensure_loaded()
        self._remember(entry)

    def record_rejected(self, entry: ImageLedgerEntry) -> None:
        """Remember a permanent refusal. Same row, same key, ``rejected`` set.

        Without this a 413 costs a download and a POST on EVERY run for ever:
        nothing was written, so the next pass has no idea it has already asked.
        """
        self.record(replace(entry, rejected=True))

    def verify(
        self,
        idempotency_key: str,
        now: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Note that these exact bytes were re-checked and are still delivered.

        Without this an already-uploaded asset would keep its original timestamp
        for ever, sort to the front of every later pass, and be re-examined on
        every run while the assets behind it were never reached.

        ``etag``/``last_modified`` are stored when the server supplied them.
        None leaves whatever is already there — a re-check that produced no new
        validator must not erase the one that made the check cheap.
        """
        self._ensure_loaded()
        entry = self._entries.get(idempotency_key)
        if entry is None:
            return
        self._remember(
            replace(
                entry,
                verified_at=now,
                etag=entry.etag if etag is None else etag,
                last_modified=entry.last_modified if last_modified is None else last_modified,
            )
        )

    def verify_ref(
        self,
        asset_ref: str,
        now: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Re-stamp EVERY key this asset holds. The 304 door.

        A 304 proves the bytes behind this asset are unchanged, but it carries
        no body — so no content hash, so no idempotency key to look up. The
        asset reference is the only handle the pass has, and every key filed
        under it describes bytes that were delivered at some point, so all of
        them are re-verified together. Without this a 304 would leave the asset
        with its old timestamp, sorting it to the front of the next pass for
        ever: the same non-convergence the deadline and the ordering exist to
        prevent.
        """
        self._ensure_loaded()
        for key in tuple(self._keys_by_ref.get(asset_ref, ())):
            self.verify(key, now, etag=etag, last_modified=last_modified)

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
        self._keys_by_ref = {}
        for entry in kept.values():
            self._remember(entry)

    def flush(self) -> None:
        """Write the full ledger back in one call. No-op if nothing was loaded."""
        if not self._loaded:
            return
        grid: list[list[str]] = [list(_COLUMNS)]
        grid.extend(
            [
                e.idempotency_key,
                e.asset_ref,
                e.storage_path,
                e.verified_at,
                e.etag,
                e.last_modified,
                _REJECTED_MARKER if e.rejected else "",
            ]
            for e in self._entries.values()
        )
        self._store.replace_grid(_TAB_NAME, grid)

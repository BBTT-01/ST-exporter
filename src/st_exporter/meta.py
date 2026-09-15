"""The `_meta` tab: per-feed run bookkeeping, and the bundled multi-feed cursor.

`_meta` has one row per output TAB (`jobs`, `technicians`, the four `pricebook.*`
tabs and the four `financial` ones — `accounting.invoices`, `payroll.timesheets`,
`settings.businessUnits`, `reporting.jobCosts`) with columns `feed`, `last_run_at`, `last_cursor`,
`row_count`, `exporter_version`, `contract_version`. TradeRated reads it
for reconciliation and support.

`contract_version` is the guard every consumer checks BEFORE parsing a tab: on a
version outside the range it understands it must stop with a named error, not
parse optimistically and not return an empty result (see `docs/export-contract.md`
and `st_exporter.contracts`).

`last_cursor` for the `jobs` feed is not a single ServiceTitan continuation token —
it's a JSON object bundling the five underlying feeds' tokens (customers, locations,
jobs, appointments, assignments), because the `jobs` tab is denormalised from all
five. This is the only place that bundling is encoded/decoded; nothing else needs to
know `last_cursor` is structured.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

META_COLUMNS: tuple[str, ...] = (
    "feed",
    "last_run_at",
    "last_cursor",
    "row_count",
    "exporter_version",
    "contract_version",
)

# The high-volume, cursor-tracked feeds that the `jobs` output feed is
# denormalised from. Order is stable so the encoded cursor is deterministic.
#
# `customer-contacts` is only ever advanced on the opt-in bulk contacts route
# (EXPORTER_CONTACTS_ROUTE=export, see feeds/contacts.py); on the default
# per-customer route it stays null, which is also exactly what a tenant switching
# TO the bulk route needs — a null token means "drain from the beginning".
JOBS_CURSOR_FEEDS: tuple[str, ...] = (
    "customers",
    "locations",
    "jobs",
    "appointments",
    "assignments",
    "customer-contacts",
)


@dataclass
class MetaRow:
    feed: str
    last_run_at: str
    last_cursor: str = ""
    row_count: int = 0
    exporter_version: str = ""
    # The tab contract this row's feed was written against — "jobs.v2",
    # "technicians.v1", "pricebook.v1", "financial.v1". Every feed declares one;
    # `st_exporter.contracts` is where they are defined and what forces a bump.
    #
    # Blank means "written by an exporter of 0.2.8 or older, which declared no
    # version at all". A consumer must treat blank as its OWN case: for `jobs` it
    # specifically means the pre-0.2.7 one-row-per-appointment shape (`jobs.v1`)
    # *or* the current one, indistinguishably. Never collapse blank with an
    # unrecognised version, and never guess it is the current one.
    contract_version: str = ""


class MetaRowSet:
    """The `_meta` rows one run will write: **exactly one per tab, by construction**.

    A run reaches a tab's `_meta` row by two different doors — a tab that was
    refreshed writes a fresh row, and a tab that was skipped (not selected this
    run, failed, or refused with a 403) carries its previous row forward — and
    for a while those doors were a plain ``list.append`` each. Nothing stopped
    both from firing for one tab, and nothing downstream noticed: `_meta` is
    parsed last-wins, so a duplicated tab silently answered with whichever row
    sorted last. A consumer following `docs/export-contract.md` and reading
    `last_run_at` for freshness would have been handed a day-old timestamp on a
    tab that had just been rewritten.

    So the collection is keyed, not appended. ``add`` is the fresh row and always
    wins; ``carry`` is the previous row and never displaces one. Duplicates are
    not detected here, they are unrepresentable.
    """

    def __init__(self) -> None:
        self._rows: dict[str, MetaRow] = {}

    def add(self, row: MetaRow) -> None:
        """Record a row this run WROTE. Replaces anything carried for that tab."""
        self._rows[row.feed] = row

    def carry(self, row: MetaRow) -> None:
        """Carry a previous row forward — unless this run already wrote a fresh one."""
        self._rows.setdefault(row.feed, row)

    def snapshot(self) -> dict[str, MetaRow]:
        """The rows recorded so far, for a caller that may have to undo the rest.

        A feed is guarded (``run._guarded_feed``, ``run._TabGuard``): if it throws
        part-way, anything it recorded describes a tab that is not on disk, and a
        cursor describing a tab that was never written is the one failure worse
        than a re-drain. Take this before running a feed and hand it to
        ``restore`` if the feed fails.
        """
        return dict(self._rows)

    def restore(self, snapshot: dict[str, MetaRow]) -> None:
        """Roll back to ``snapshot``, discarding every row recorded since.

        Called on the failure path only, and BEFORE the failed tab's previous row
        is carried forward — ``carry`` never displaces an existing row, so a
        half-written fresh row left in place would silently beat the carried one.
        """
        self._rows = dict(snapshot)

    def __contains__(self, feed: object) -> bool:
        return feed in self._rows

    def __iter__(self) -> "Iterator[MetaRow]":
        return iter(self._rows.values())

    def __len__(self) -> int:
        return len(self._rows)


@dataclass
class CursorBundle:
    """The set of continuation tokens the `jobs` feed depends on."""

    tokens: dict[str, str | None] = field(default_factory=dict)

    def get(self, feed: str) -> str | None:
        return self.tokens.get(feed)

    def with_token(self, feed: str, token: str | None) -> "CursorBundle":
        updated = dict(self.tokens)
        updated[feed] = token
        return CursorBundle(updated)

    def encode(self) -> str:
        return json.dumps(self.tokens, sort_keys=True)

    @classmethod
    def decode(cls, raw: str | None) -> "CursorBundle":
        if not raw:
            return cls({feed: None for feed in JOBS_CURSOR_FEEDS})
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError:
            return cls({feed: None for feed in JOBS_CURSOR_FEEDS})
        if not isinstance(data, dict):
            # Valid JSON but not an object (e.g. a list, string, or number) — a
            # hand-edited or partially-written cell. Same fallback as bad JSON:
            # treat as "no prior cursor" rather than crashing at the top of every
            # run, which would be a permanent, self-inflicted outage.
            return cls({feed: None for feed in JOBS_CURSOR_FEEDS})
        return cls({feed: data.get(feed) for feed in JOBS_CURSOR_FEEDS})


def parse_meta_grid(grid: list[list[str]]) -> dict[str, MetaRow]:
    """Parse a `_meta` tab grid (header row + data rows) into feed -> MetaRow."""
    if not grid:
        return {}
    header, *data_rows = grid
    index = {name: i for i, name in enumerate(header)}
    rows: dict[str, MetaRow] = {}
    for data_row in data_rows:
        if not data_row or not data_row[0]:
            continue

        def cell(col: str) -> str:
            i = index.get(col)
            return data_row[i] if i is not None and i < len(data_row) else ""

        feed = cell("feed")
        rows[feed] = MetaRow(
            feed=feed,
            last_run_at=cell("last_run_at"),
            last_cursor=cell("last_cursor"),
            row_count=_parse_row_count(cell("row_count")),
            exporter_version=cell("exporter_version"),
            contract_version=cell("contract_version"),
        )
    return rows


def _parse_row_count(raw: str) -> int:
    """Defensive int parse — a hand-edited or corrupted ``_meta`` cell must not
    crash the run before any work happens, matching ``CursorBundle.decode`` and
    ``RawCache.from_grid``'s tolerance of bad data elsewhere in this module."""
    try:
        return int(raw or 0)
    except ValueError:
        return 0


def build_meta_grid(rows: Iterable[MetaRow]) -> list[list[str]]:
    """Render `_meta` rows (sorted by feed name for determinism) into a full grid.

    Refuses two rows for one tab. `_meta` is parsed last-wins, so a duplicate does
    not read as an error downstream — it reads as the WRONG ROW, quietly, and
    `docs/export-contract.md` tells consumers to trust `last_run_at` for
    freshness. ``MetaRowSet`` already makes that unrepresentable for a real run;
    this is the assertion at the boundary, so any future caller that hand-rolls a
    list gets a crash instead of a stale timestamp.
    """
    materialised = list(rows)
    counts = Counter(row.feed for row in materialised)
    duplicates = sorted(feed for feed, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(
            "_meta would have more than one row for: "
            + ", ".join(duplicates)
            + ". A tab has exactly one row; last-wins parsing would silently pick one."
        )
    grid: list[list[str]] = [list(META_COLUMNS)]
    for row in sorted(materialised, key=lambda r: r.feed):
        grid.append(
            [
                row.feed,
                row.last_run_at,
                row.last_cursor,
                str(row.row_count),
                row.exporter_version,
                row.contract_version,
            ]
        )
    return grid

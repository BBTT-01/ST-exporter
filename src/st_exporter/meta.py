"""The `_meta` tab: per-feed run bookkeeping, and the bundled multi-feed cursor.

`_meta` has one row per output feed (`jobs`, `technicians`) with columns `feed`,
`last_run_at`, `last_cursor`, `row_count`, `exporter_version`. TradeRated reads it
for reconciliation and support.

`last_cursor` for the `jobs` feed is not a single ServiceTitan continuation token —
it's a JSON object bundling the five underlying feeds' tokens (customers, locations,
jobs, appointments, assignments), because the `jobs` tab is denormalised from all
five. This is the only place that bundling is encoded/decoded; nothing else needs to
know `last_cursor` is structured.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

META_COLUMNS: tuple[str, ...] = (
    "feed",
    "last_run_at",
    "last_cursor",
    "row_count",
    "exporter_version",
)

# The five high-volume, cursor-tracked feeds that the `jobs` output feed is
# denormalised from. Order is stable so the encoded cursor is deterministic.
JOBS_CURSOR_FEEDS: tuple[str, ...] = (
    "customers",
    "locations",
    "jobs",
    "appointments",
    "assignments",
)


@dataclass
class MetaRow:
    feed: str
    last_run_at: str
    last_cursor: str = ""
    row_count: int = 0
    exporter_version: str = ""


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
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
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
            row_count=int(cell("row_count") or 0),
            exporter_version=cell("exporter_version"),
        )
    return rows


def build_meta_grid(rows: list[MetaRow]) -> list[list[str]]:
    """Render `_meta` rows (sorted by feed name for determinism) into a full grid."""
    grid: list[list[str]] = [list(META_COLUMNS)]
    for row in sorted(rows, key=lambda r: r.feed):
        grid.append(
            [
                row.feed,
                row.last_run_at,
                row.last_cursor,
                str(row.row_count),
                row.exporter_version,
            ]
        )
    return grid

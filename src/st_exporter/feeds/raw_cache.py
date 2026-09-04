"""Generic id -> full-record cache, persisted as a two-column `id, payload_json` tab.

Used for the five high-volume, cursor-tracked feeds (customers, locations, jobs,
appointments, assignments) in the *private* raw-cache Sheet — never the
customer-facing Export Store. Each tab stores the full raw ServiceTitan record per
id so a delta fetch can be merged in (last-write-wins) and the whole set can be
freshly re-denormalised and re-windowed every run. See `run.py` for why a full
in-memory snapshot is needed even though fetches are incremental.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

RAW_CACHE_COLUMNS: tuple[str, ...] = ("id", "payload_json")


@dataclass
class RawCache:
    records: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, record_id: Any) -> dict[str, Any] | None:
        return self.records.get(str(record_id))

    def merge(self, delta: list[dict[str, Any]], id_field: str = "id") -> None:
        """Overwrite by id. A change-feed record represents current state for that
        id, so last-write-wins is correct — there's no need to diff field by field."""
        for record in delta:
            record_id = record.get(id_field)
            if record_id is None:
                continue
            self.records[str(record_id)] = record

    def values(self) -> list[dict[str, Any]]:
        return list(self.records.values())

    def keep(self, ids: set[str]) -> None:
        """Drop every record not in ``ids``, bounding the cache to what's currently
        relevant instead of growing forever. Safe: if a dropped record changes
        again later, ServiceTitan's change feed resends it in a future delta and
        ``merge()`` re-adds it — nothing already retained is permanently lost."""
        self.records = {rid: r for rid, r in self.records.items() if rid in ids}

    def to_grid(self) -> list[list[str]]:
        grid: list[list[str]] = [list(RAW_CACHE_COLUMNS)]
        for record_id in sorted(self.records):
            payload = json.dumps(self.records[record_id], sort_keys=True, default=str)
            grid.append([record_id, payload])
        return grid

    @classmethod
    def from_grid(cls, grid: list[list[str]]) -> "RawCache":
        if not grid:
            return cls()
        _, *data_rows = grid
        records: dict[str, dict[str, Any]] = {}
        for row in data_rows:
            if len(row) < 2 or not row[0]:
                continue
            try:
                records[row[0]] = json.loads(row[1])
            except json.JSONDecodeError:
                continue
        return cls(records)

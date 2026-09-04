"""Google Sheets writer: credentials from env only, full-tab-replace as one call.

Two spreadsheets are involved, both accessed through this same ``SheetsClient``:
the customer-facing Export Store (shared read-only with TradeRated — `jobs`,
`technicians`, `_meta` only) and a private raw-cache spreadsheet the exporter's
service account owns but never shares (`_raw_customers`, `_raw_locations`, etc.) —
see the plan's "raw-cache placement" decision for why those live separately.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, cast

import gspread
from gspread.utils import rowcol_to_a1

from st_cli.exceptions import ConfigError

_SPREADSHEET_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
_NEW_WORKSHEET_ROWS = 1
_NEW_WORKSHEET_COLS = 1


def get_gspread_client(raw_service_account_json: str) -> gspread.Client:
    """Build an authorized gspread client from a service-account JSON blob in env.

    Never writes the credential to disk — ``from_service_account_info`` (via
    ``service_account_from_dict``) takes the parsed dict directly. The parse error
    path deliberately omits the raw JSON from the raised exception so a bad env var
    can't leak its own (still-secret) contents into a log or traceback.
    """
    try:
        info = json.loads(raw_service_account_json)
    except json.JSONDecodeError as exc:
        raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
    return gspread.service_account_from_dict(info, scopes=list(_SPREADSHEET_SCOPES))


class SheetsPort(Protocol):
    """The minimal interface ``run.py`` depends on — implemented by both the real
    gspread-backed ``SheetsClient`` and the in-memory fake used in tests."""

    def read_grid(self, tab_name: str) -> list[list[str]]: ...

    def replace_grid(self, tab_name: str, grid: list[list[str]]) -> None: ...


class SheetsClient:
    """Wraps one Google Spreadsheet: read a tab as a grid, or fully replace one."""

    def __init__(self, spreadsheet: Any) -> None:
        self._spreadsheet = spreadsheet

    @classmethod
    def open(cls, client: gspread.Client, sheet_id: str) -> "SheetsClient":
        return cls(client.open_by_key(sheet_id))

    def read_grid(self, tab_name: str) -> list[list[str]]:
        try:
            worksheet = self._spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            return []
        return cast("list[list[str]]", worksheet.get_all_values())

    def replace_grid(self, tab_name: str, grid: list[list[str]]) -> None:
        """Full-replace ``tab_name`` with ``grid`` in one HTTP call.

        A naive clear-then-write is exactly the partial-write state "full replace"
        is meant to avoid — an external reader could see an empty tab between the
        two calls. Instead this issues a single ``batchUpdate``: one range for the
        new grid, plus (only if the new grid is smaller) ranges blanking the now-
        stale trailing rows and/or columns — so there is never a visible
        intermediate empty-tab state, and no leftover cell from a previous, larger
        run survives anywhere in the sheet.

        Note: ``grid`` must have at least one row (a header row, at minimum) —
        every caller in this package guarantees that. An empty grid raises from
        ``rowcol_to_a1(0, ...)`` rather than silently writing nothing; that's
        intentional (a would-be-empty-tab write is far more likely a bug upstream
        than a real "zero rows" case) and is covered by a regression test.
        """
        new_row_count = len(grid)
        new_col_count = max((len(row) for row in grid), default=1)

        worksheet = self._get_or_create_worksheet(tab_name, new_row_count, new_col_count)
        old_row_count = worksheet.row_count
        old_col_count = worksheet.col_count
        max_row_count = max(old_row_count, new_row_count)
        max_col_count = max(old_col_count, new_col_count)

        if old_row_count < new_row_count or old_col_count < new_col_count:
            worksheet.resize(rows=max_row_count, cols=max_col_count)

        updates: list[dict[str, Any]] = [
            {
                "range": f"A1:{rowcol_to_a1(new_row_count, new_col_count)}",
                "values": grid,
            }
        ]
        if old_row_count > new_row_count:
            # Stale trailing rows from a previous, longer run — blank across the
            # full old width so no stale cell survives in a row the new grid no
            # longer has.
            blank_rows = max_row_count - new_row_count
            blank_grid = [[""] * max_col_count for _ in range(blank_rows)]
            start_a1 = rowcol_to_a1(new_row_count + 1, 1)
            end_a1 = rowcol_to_a1(max_row_count, max_col_count)
            updates.append({"range": f"{start_a1}:{end_a1}", "values": blank_grid})
        if old_col_count > new_col_count and new_row_count > 0:
            # Stale trailing columns from a previous, wider run — blank within the
            # rows the new grid actually has (rows beyond that are already fully
            # blanked, full old width, by the row branch above).
            blank_cols = max_col_count - new_col_count
            blank_grid = [[""] * blank_cols for _ in range(new_row_count)]
            start_a1 = rowcol_to_a1(1, new_col_count + 1)
            end_a1 = rowcol_to_a1(new_row_count, max_col_count)
            updates.append({"range": f"{start_a1}:{end_a1}", "values": blank_grid})

        worksheet.batch_update(updates, raw=True)

    def _get_or_create_worksheet(self, tab_name: str, rows: int, cols: int) -> Any:
        try:
            return self._spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            return self._spreadsheet.add_worksheet(
                title=tab_name,
                rows=max(rows, _NEW_WORKSHEET_ROWS),
                cols=max(cols, _NEW_WORKSHEET_COLS),
            )


class InMemorySheetsStore:
    """A ``SheetsPort`` backed by a plain dict of grids — no network at all.

    Used for ``st-export --dry-run`` (compute and print a run's output without any
    Google Sheets access) and as the fake double in ``tests/st_exporter``'s
    fixture-driven integration test, so that test exercises the entire orchestration
    in ``run.py`` without mocking gspread call-by-call.
    """

    def __init__(self) -> None:
        self._tabs: dict[str, list[list[str]]] = {}

    def read_grid(self, tab_name: str) -> list[list[str]]:
        return [row[:] for row in self._tabs.get(tab_name, [])]

    def replace_grid(self, tab_name: str, grid: list[list[str]]) -> None:
        self._tabs[tab_name] = [row[:] for row in grid]

    @property
    def tabs(self) -> dict[str, list[list[str]]]:
        return self._tabs

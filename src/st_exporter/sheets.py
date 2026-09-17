"""Google Sheets writer: credentials from env only, full-tab-replace per tab.

Two spreadsheets are involved, both accessed through this same ``SheetsClient``:
the customer-facing Export Store (shared read-only with TradeRated — `jobs`,
`technicians`, `_meta` only) and a private raw-cache spreadsheet the exporter's
service account owns but never shares (`_raw_customers`, `_raw_locations`, etc.) —
see the plan's "raw-cache placement" decision for why those live separately.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Protocol, cast

import gspread
from gspread.utils import rowcol_to_a1

from st_cli.exceptions import ConfigError, SheetsCapacityError
from st_exporter.logging_setup import logger

_SPREADSHEET_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
_NEW_WORKSHEET_ROWS = 1
_NEW_WORKSHEET_COLS = 1

#: Google answers these for reasons that have nothing to do with the request —
#: `500 Internal error encountered` on a large ``batchUpdate`` (Pro Garage Doors,
#: run 35248585567, a 9917-row services tab), 502/503 during an incident, 429 when
#: the per-minute write quota is momentarily spent. Google's own guidance for all
#: four is "back off and retry". Anything else — 400 for a malformed range, 403 for
#: a Sheet the writer cannot edit, 404 for a spreadsheet id that is wrong — is the
#: request's fault and retrying it would only repeat the same answer four times.
_TRANSIENT_SHEETS_CODES = frozenset({429, 500, 502, 503})
_SHEETS_WRITE_ATTEMPTS = 4
_SHEETS_BACKOFF_BASE_SECONDS = 2.0

#: The Sheets API has no documented per-request cell limit, but a single
#: ``values.update``/``batchUpdate`` covering millions of cells is what produced
#: run 35260724032's "Unable to parse range" failure on pw-pro-garage-doors'
#: 99,179-row, 42-column `pricebook.services` tab (4,165,518 cells) — a smaller
#: batchUpdate would have carried the exact same range and values without it.
#: 200,000 cells keeps every request comfortably inside a normal JSON body size
#: regardless of column count.
_MAX_CELLS_PER_WRITE = 200_000

#: Google Sheets' own hard cap: a spreadsheet may hold at most this many cells,
#: shared across every tab it contains (see docs/export-contract.md's "Size").
_SPREADSHEET_CELL_CAP = 10_000_000


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

    def __init__(self, spreadsheet: Any, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._spreadsheet = spreadsheet
        self._sleep = sleep

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
        """Full-replace ``tab_name`` with ``grid``, chunked and retried as needed.

        A naive clear-then-write is exactly the partial-write state "full replace"
        is meant to avoid — an external reader could see an empty tab between the
        two calls. For a tab small enough to fit in one request (every tab today
        except a handful of large-catalogue `pricebook.services` tenants) this
        issues a single ``batchUpdate``: one range for the new grid, plus (only if
        the new grid is smaller) ranges blanking the now-stale trailing rows
        and/or columns — so there is never a visible intermediate empty-tab
        state, and no leftover cell from a previous, larger run survives anywhere
        in the sheet.

        Once the new grid's cells pass ``_MAX_CELLS_PER_WRITE`` (run 35260724032:
        99,179 rows x 42 cols = 4,165,518 cells produced a "500 Internal error
        encountered" followed by an "Unable to parse range" once retried), each
        region above is instead split into row-chunks and written as its own
        ``batchUpdate`` call — still full-replace in intent (every chunk lands
        before this method returns, or it raises and no more chunks are sent),
        just no longer a single HTTP request large enough for Google to refuse.

        Every mutating call — the resize and each write, chunked or not — retries
        on a transient Sheets error via ``_retry_transient``. If a chunk still
        fails after retries, this raises and no later chunk is written: the
        caller (``run.py``'s ``_TabGuard``) never writes `_meta` for a run whose
        tab write raised, so a partially-written tab is never reported fresh —
        the same ordering guarantee `docs/export-contract.md` already documents
        for a tab that fails outright.

        Before touching the API at all, this checks whether writing ``grid``
        would push the SPREADSHEET (not just this tab) over Google's
        ``_SPREADSHEET_CELL_CAP``-cell limit, and raises ``SheetsCapacityError``
        naming the tab, its size and the cap — rather than letting the resize (or
        the write itself) fail with gspread's opaque "Unable to parse range".

        Note: ``grid`` must have at least one row (a header row, at minimum) —
        every caller in this package guarantees that. An empty grid raises from
        ``rowcol_to_a1(0, ...)`` rather than silently writing nothing; that's
        intentional (a would-be-empty-tab write is far more likely a bug upstream
        than a real "zero rows" case) and is covered by a regression test.
        """
        new_row_count = len(grid)
        new_col_count = max((len(row) for row in grid), default=1)

        self._check_spreadsheet_capacity(tab_name, new_row_count, new_col_count)

        worksheet = self._get_or_create_worksheet(tab_name, new_row_count, new_col_count)
        old_row_count = worksheet.row_count
        old_col_count = worksheet.col_count
        max_row_count = max(old_row_count, new_row_count)
        max_col_count = max(old_col_count, new_col_count)

        if old_row_count < new_row_count or old_col_count < new_col_count:
            self._retry_transient(
                lambda: worksheet.resize(rows=max_row_count, cols=max_col_count),
                what=f"resize {tab_name} to {max_row_count}x{max_col_count}",
            )

        regions: list[tuple[int, int, list[list[str]]]] = [(1, 1, grid)]
        if old_row_count > new_row_count:
            # Stale trailing rows from a previous, longer run — blank across the
            # full old width so no stale cell survives in a row the new grid no
            # longer has.
            blank_rows = max_row_count - new_row_count
            blank_grid = [[""] * max_col_count for _ in range(blank_rows)]
            regions.append((new_row_count + 1, 1, blank_grid))
        if old_col_count > new_col_count and new_row_count > 0:
            # Stale trailing columns from a previous, wider run — blank within the
            # rows the new grid actually has (rows beyond that are already fully
            # blanked, full old width, by the row branch above).
            blank_cols = max_col_count - new_col_count
            blank_grid = [[""] * blank_cols for _ in range(new_row_count)]
            regions.append((1, new_col_count + 1, blank_grid))

        total_cells = new_row_count * new_col_count
        if total_cells <= _MAX_CELLS_PER_WRITE:
            updates = [
                _region_update(start_row, start_col, values)
                for start_row, start_col, values in regions
            ]
            cells = sum(len(row) for row in grid)
            what = f"write {tab_name} ({new_row_count} rows x {new_col_count} cols, {cells} cells)"
            self._retry_transient(
                lambda: worksheet.batch_update(updates, raw=True),
                what=what,
            )
            return

        for start_row, start_col, values in regions:
            chunks = _chunk_region(start_row, start_col, values, _MAX_CELLS_PER_WRITE)
            for index, update in enumerate(chunks, start=1):
                chunk_rows = len(update["values"])
                chunk_cols = len(update["values"][0]) if update["values"] else 0

                def _write_this_chunk() -> None:
                    worksheet.batch_update([update], raw=True)

                # Called synchronously, within this same iteration, by
                # `_retry_transient` below — never stored for later — so the
                # usual late-binding closure trap (every callback sharing the
                # LAST loop value) does not apply here.
                self._retry_transient(
                    _write_this_chunk,
                    what=(
                        f"write {tab_name} chunk {index}/{len(chunks)} "
                        f"({update['range']}, {chunk_rows * chunk_cols} cells)"
                    ),
                )

    def _check_spreadsheet_capacity(
        self, tab_name: str, new_row_count: int, new_col_count: int
    ) -> None:
        """Refuse a write that would push the SPREADSHEET over Google's cap.

        The cap is shared across every tab, so this sums every OTHER tab's
        current grid size (rows x cols — that is what Google counts against the
        cap, not just populated cells) and adds what this tab's write would make
        it. This tab's own PREVIOUS size is excluded: it is going away the moment
        this write succeeds, so counting it too would refuse a same-size or
        shrinking rewrite of a tab that is itself the reason the cap is close.
        """
        other_cells = sum(
            worksheet.row_count * worksheet.col_count
            for worksheet in self._spreadsheet.worksheets()
            if worksheet.title != tab_name
        )
        projected = other_cells + (new_row_count * new_col_count)
        if projected > _SPREADSHEET_CELL_CAP:
            raise SheetsCapacityError(tab_name, new_row_count, new_col_count, _SPREADSHEET_CELL_CAP)

    def _retry_transient(self, op: Callable[[], Any], *, what: str) -> Any:
        """Run one Sheets call, retrying only Google's transient answers.

        This retries the SAME call ``op`` was given — the same ``batchUpdate``
        (whole tab, or one chunk of it) each time, never a different one — so a
        reader still never sees a half-written REGION appear over multiple
        differently-sized attempts. The size is in ``what`` on purpose — if a tab
        (or a chunk of one) starts failing on every attempt, the first question
        is whether it is a transient or a size problem, and the run log should be
        able to answer it.
        """
        for attempt in range(1, _SHEETS_WRITE_ATTEMPTS + 1):
            try:
                return op()
            except gspread.exceptions.APIError as exc:
                code = _sheets_status(exc)
                if code not in _TRANSIENT_SHEETS_CODES or attempt == _SHEETS_WRITE_ATTEMPTS:
                    raise
                wait = _SHEETS_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Google Sheets answered %s on %s — retrying in %.0fs (attempt %d of %d)",
                    code,
                    what,
                    wait,
                    attempt,
                    _SHEETS_WRITE_ATTEMPTS,
                )
                self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    def _get_or_create_worksheet(self, tab_name: str, rows: int, cols: int) -> Any:
        try:
            return self._spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            return self._spreadsheet.add_worksheet(
                title=tab_name,
                rows=max(rows, _NEW_WORKSHEET_ROWS),
                cols=max(cols, _NEW_WORKSHEET_COLS),
            )


def _region_update(start_row: int, start_col: int, values: list[list[str]]) -> dict[str, Any]:
    """One ``batchUpdate`` range dict covering ``values`` starting at (row, col).

    Shared by the single-call path (whole tab) and the chunked path (one row
    slice of it) so both compute the A1 range the exact same way.
    """
    height = len(values)
    width = max((len(row) for row in values), default=0)
    end_row = start_row + height - 1
    end_col = start_col + width - 1
    start_a1 = rowcol_to_a1(start_row, start_col)
    end_a1 = rowcol_to_a1(end_row, end_col)
    return {"range": f"{start_a1}:{end_a1}", "values": values}


def _chunk_region(
    start_row: int, start_col: int, values: list[list[str]], max_cells: int
) -> list[dict[str, Any]]:
    """Split one region into row-slices of at most ``max_cells`` each.

    The slice size is derived from the region's own width, so a wide tab gets
    fewer rows per chunk than a narrow one for the same cell budget — the ask
    was "≤200,000 cells per call", not "≤200,000 rows per call".
    """
    if not values:
        return []
    width = max((len(row) for row in values), default=0)
    if width == 0:
        return []
    rows_per_chunk = max(1, max_cells // width)
    return [
        _region_update(start_row + offset, start_col, values[offset : offset + rows_per_chunk])
        for offset in range(0, len(values), rows_per_chunk)
    ]


def _sheets_status(exc: gspread.exceptions.APIError) -> int:
    """The HTTP status behind a gspread ``APIError``.

    gspread reads ``code`` out of the JSON error body and falls back to ``-1`` when
    the body is not JSON — which is exactly what a 502/503 from a Google front end
    looks like — so the response's own status code is preferred when it is there.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return int(getattr(exc, "code", -1))


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

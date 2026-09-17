"""Tests for SheetsClient — the real gspread-backed writer.

Everything else in this package tests against InMemorySheetsStore, which shares
no code with SheetsClient. These tests exercise the real resize/range-arithmetic/
create-if-missing logic directly, using a MagicMock worksheet/spreadsheet, so a
mistake here fails a test rather than corrupting the real customer-facing Sheet.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import gspread
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from st_exporter.sheets import SheetsClient, get_gspread_client


def _fake_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture()
def worksheet():
    ws = MagicMock()
    ws.row_count = 1
    ws.col_count = 1
    return ws


@pytest.fixture()
def spreadsheet(worksheet):
    ss = MagicMock()
    ss.worksheet.return_value = worksheet
    return ss


class TestGetGspreadClient:
    def test_valid_json_builds_a_client(self) -> None:
        info = {
            "type": "service_account",
            "project_id": "p",
            "private_key": _fake_private_key_pem(),
            "client_email": "svc@p.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        client = get_gspread_client(json.dumps(info))
        assert isinstance(client, gspread.Client)

    def test_invalid_json_raises_config_error_without_echoing_the_raw_value(self) -> None:
        from st_cli.exceptions import ConfigError

        raw = "definitely not json {{{"
        with pytest.raises(ConfigError) as exc_info:
            get_gspread_client(raw)
        assert raw not in str(exc_info.value)


class TestReadGrid:
    def test_returns_all_values_for_an_existing_worksheet(self, spreadsheet, worksheet) -> None:
        worksheet.get_all_values.return_value = [["a", "b"], ["1", "2"]]
        client = SheetsClient(spreadsheet)
        assert client.read_grid("jobs") == [["a", "b"], ["1", "2"]]
        spreadsheet.worksheet.assert_called_once_with("jobs")

    def test_missing_worksheet_returns_empty_list(self, spreadsheet) -> None:
        spreadsheet.worksheet.side_effect = gspread.WorksheetNotFound("nope")
        client = SheetsClient(spreadsheet)
        assert client.read_grid("_meta") == []


class TestReplaceGrid:
    def test_writes_the_full_grid_to_a1_range(self, spreadsheet, worksheet) -> None:
        worksheet.row_count = 3
        worksheet.col_count = 2
        client = SheetsClient(spreadsheet)
        grid = [["h1", "h2"], ["a", "b"], ["c", "d"]]

        client.replace_grid("jobs", grid)

        worksheet.batch_update.assert_called_once()
        (updates,), kwargs = worksheet.batch_update.call_args
        assert kwargs == {"raw": True}
        assert updates[0] == {"range": "A1:B3", "values": grid}

    def test_resizes_up_when_new_grid_is_larger(self, spreadsheet, worksheet) -> None:
        worksheet.row_count = 1
        worksheet.col_count = 1
        client = SheetsClient(spreadsheet)
        grid = [["h1", "h2", "h3"], ["a", "b", "c"]]

        client.replace_grid("jobs", grid)

        worksheet.resize.assert_called_once_with(rows=2, cols=3)

    def test_does_not_resize_when_new_grid_fits(self, spreadsheet, worksheet) -> None:
        worksheet.row_count = 10
        worksheet.col_count = 10
        client = SheetsClient(spreadsheet)

        client.replace_grid("jobs", [["h1"], ["a"]])

        worksheet.resize.assert_not_called()

    def test_blanks_stale_trailing_rows_across_the_full_old_width(
        self, spreadsheet, worksheet
    ) -> None:
        worksheet.row_count = 5
        worksheet.col_count = 4
        client = SheetsClient(spreadsheet)
        grid = [["h1", "h2"], ["a", "b"]]  # 2 rows, was 5; 2 cols, was 4

        client.replace_grid("jobs", grid)

        (updates,), _ = worksheet.batch_update.call_args
        row_blank = next(u for u in updates if u["range"] == "A3:D5")
        assert row_blank["values"] == [["", "", "", ""]] * 3

    def test_blanks_stale_trailing_columns_within_the_new_row_range(
        self, spreadsheet, worksheet
    ) -> None:
        worksheet.row_count = 2
        worksheet.col_count = 5
        client = SheetsClient(spreadsheet)
        grid = [["h1", "h2"], ["a", "b"]]  # 2 rows (matches old), 2 cols, was 5

        client.replace_grid("jobs", grid)

        (updates,), _ = worksheet.batch_update.call_args
        col_blank = next(u for u in updates if u["range"] == "C1:E2")
        assert col_blank["values"] == [["", "", ""]] * 2

    def test_no_blank_ranges_when_grid_is_the_same_size(self, spreadsheet, worksheet) -> None:
        worksheet.row_count = 2
        worksheet.col_count = 2
        client = SheetsClient(spreadsheet)

        client.replace_grid("jobs", [["h1", "h2"], ["a", "b"]])

        (updates,), _ = worksheet.batch_update.call_args
        assert len(updates) == 1

    def test_creates_the_worksheet_when_missing(self, spreadsheet) -> None:
        new_ws = MagicMock()
        new_ws.row_count = 1
        new_ws.col_count = 1
        spreadsheet.worksheet.side_effect = gspread.WorksheetNotFound("nope")
        spreadsheet.add_worksheet.return_value = new_ws
        client = SheetsClient(spreadsheet)

        client.replace_grid("jobs", [["h1", "h2"], ["a", "b"]])

        spreadsheet.add_worksheet.assert_called_once_with(title="jobs", rows=2, cols=2)
        new_ws.batch_update.assert_called_once()

    def test_empty_grid_raises_rather_than_writing_nothing(self, spreadsheet, worksheet) -> None:
        # Documented, intentional behavior (see replace_grid's docstring): every
        # real caller in this package prepends a header row, so an empty grid is
        # far more likely a bug upstream than a genuine "zero rows" case.
        client = SheetsClient(spreadsheet)
        with pytest.raises(gspread.exceptions.IncorrectCellLabel):
            client.replace_grid("jobs", [])


def _api_error(
    status: int, message: str = "Internal error encountered."
) -> gspread.exceptions.APIError:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {"error": {"code": status, "message": message, "status": "X"}}
    response.text = message
    return gspread.exceptions.APIError(response)


class TestReplaceGridRetriesTransientGoogleErrors:
    """Pro Garage Doors run 35248585567: Google answered `500 Internal error
    encountered` on the one batchUpdate carrying a 9917-row services tab, and the
    whole pricebook run went red with nothing tried twice. Google's guidance for
    429/500/502/503 is back off and retry; everything else is the request's fault."""

    def _client(self, spreadsheet) -> tuple[SheetsClient, list[float]]:
        waits: list[float] = []
        return SheetsClient(spreadsheet, sleep=waits.append), waits

    def test_a_500_is_retried_and_the_same_write_then_succeeds(
        self, spreadsheet, worksheet, caplog
    ) -> None:
        worksheet.batch_update.side_effect = [_api_error(500), None]
        client, waits = self._client(spreadsheet)
        with caplog.at_level("WARNING", logger="st_exporter"):
            client.replace_grid("pricebook.services", [["a", "b"], ["1", "2"]])
        assert worksheet.batch_update.call_count == 2
        # The SAME batchUpdate both times — the full-replace-in-one-call design is
        # kept, nothing is split into partial writes.
        first, second = worksheet.batch_update.call_args_list
        assert first == second
        assert waits == [2.0]
        assert "Google Sheets answered 500" in caplog.text
        assert "pricebook.services" in caplog.text
        assert "4 cells" in caplog.text, "the grid's size is in the message on purpose"

    @pytest.mark.parametrize("status", [429, 502, 503])
    def test_the_other_transient_codes_are_retried_too(
        self, spreadsheet, worksheet, status
    ) -> None:
        worksheet.batch_update.side_effect = [_api_error(status), None]
        client, waits = self._client(spreadsheet)
        client.replace_grid("jobs", [["h"]])
        assert worksheet.batch_update.call_count == 2
        assert waits == [2.0]

    def test_backoff_doubles_and_the_last_failure_is_raised(self, spreadsheet, worksheet) -> None:
        worksheet.batch_update.side_effect = [_api_error(500)] * 4
        client, waits = self._client(spreadsheet)
        with pytest.raises(gspread.exceptions.APIError):
            client.replace_grid("jobs", [["h"]])
        assert worksheet.batch_update.call_count == 4
        assert waits == [2.0, 4.0, 8.0]

    @pytest.mark.parametrize("status", [400, 403, 404])
    def test_a_request_error_is_not_retried(self, spreadsheet, worksheet, status) -> None:
        """A malformed range, a Sheet the writer cannot edit, a wrong spreadsheet id:
        the answer would be the same four times over. Fail now, once."""
        worksheet.batch_update.side_effect = _api_error(status, "bad request")
        client, waits = self._client(spreadsheet)
        with pytest.raises(gspread.exceptions.APIError):
            client.replace_grid("jobs", [["h"]])
        assert worksheet.batch_update.call_count == 1
        assert waits == []

    def test_a_transient_resize_failure_is_retried_as_well(self, spreadsheet, worksheet) -> None:
        worksheet.row_count = 1
        worksheet.col_count = 1
        worksheet.resize.side_effect = [_api_error(503), None]
        client, waits = self._client(spreadsheet)
        client.replace_grid("jobs", [["a", "b"], ["1", "2"]])
        assert worksheet.resize.call_count == 2
        assert worksheet.batch_update.call_count == 1
        assert waits == [2.0]

    def test_a_non_json_502_body_is_read_off_the_response_status(
        self, spreadsheet, worksheet
    ) -> None:
        """gspread's own `code` is -1 when Google's front end answers with an HTML
        error page instead of JSON — which is what a 502 looks like."""
        response = MagicMock()
        response.status_code = 502
        response.json.side_effect = ValueError("not json")
        response.text = "<html>Bad Gateway</html>"
        worksheet.batch_update.side_effect = [gspread.exceptions.APIError(response), None]
        client, waits = self._client(spreadsheet)
        client.replace_grid("jobs", [["h"]])
        assert worksheet.batch_update.call_count == 2

    def test_the_default_sleep_is_real_time_sleep(self, spreadsheet) -> None:
        import time

        assert SheetsClient(spreadsheet)._sleep is time.sleep

"""Fetch-layer tests for `sales.estimates`: module routing and the server-side window.

The routing assertion exists because this repo has shipped exactly this bug once
already — estimates were routed to `accounting` instead of `sales` (see
`CLAUDE.md`) — a wrong module is a 404 against a real tenant and nothing at all
against a mock.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from st_exporter.feeds.sales import ESTIMATE_DATE_PARAM, fetch_estimates

TODAY = date(2026, 9, 14)


def _envelope(data, has_more=False):
    return {"data": data, "hasMore": has_more}


class TestRouting:
    def test_estimates_live_in_sales_not_accounting(self) -> None:
        client = MagicMock()
        client.get.return_value = _envelope([])
        fetch_estimates(client, today=TODAY)
        assert client.get.call_args.args[:2] == ("sales", "estimates")


class TestWindow:
    def test_the_window_is_sent_as_a_query_param_not_applied_locally(self) -> None:
        client = MagicMock()
        client.get.return_value = _envelope([])
        fetch_estimates(client, today=TODAY, window_days=90)
        params = client.get.call_args.kwargs["params"]
        assert params[ESTIMATE_DATE_PARAM] == "2026-06-16T00:00:00Z"

    def test_records_outside_the_window_trip_the_warn_tripwire(self, caplog) -> None:
        client = MagicMock()
        client.get.return_value = _envelope([{"id": 1, "modifiedOn": "2020-01-01T00:00:00Z"}])
        fetch_estimates(client, today=TODAY, window_days=90)
        assert "predates the requested window" in caplog.text

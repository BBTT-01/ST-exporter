"""Tests for TradeRatedOutboxClient — the claim/report HTTP calls.

The exact response envelope (`GET /crm-outbox`'s shape beyond the per-item
{id, idempotency_key, kind, payload} the spec names) is not settled — see
KNOWN_UNVERIFIED.md. These tests fix the assumption this client makes
(`{"items": [...]}`) so a future correction is a one-place diff.
"""

from __future__ import annotations

import json
from typing import Generator

import httpx
import pytest
import respx

from st_exporter.outbox.client import OutboxItem, TradeRatedOutboxClient

BASE_URL = "https://outbox.example.com"


@pytest.fixture()
def client() -> Generator[TradeRatedOutboxClient, None, None]:
    c = TradeRatedOutboxClient(BASE_URL, "test-machine-token")
    yield c
    c.close()


class TestClaim:
    @respx.mock
    def test_claim_returns_parsed_items(self, client: TradeRatedOutboxClient) -> None:
        respx.get(f"{BASE_URL}/crm-outbox").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "1",
                            "idempotency_key": "key-1",
                            "kind": "referral_lead",
                            "payload": {"name": "Jane"},
                        }
                    ]
                },
            )
        )
        items = client.claim(limit=5)
        assert items == [
            OutboxItem(
                id="1",
                idempotency_key="key-1",
                kind="referral_lead",
                payload={"name": "Jane"},
            )
        ]

    @respx.mock
    def test_claim_sends_bearer_token_and_limit(self, client: TradeRatedOutboxClient) -> None:
        route = respx.get(f"{BASE_URL}/crm-outbox").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        client.claim(limit=7)
        request = route.calls.last.request
        assert request.headers["Authorization"] == "Bearer test-machine-token"
        assert request.url.params["limit"] == "7"

    @respx.mock
    def test_claim_empty_items_defaults_to_empty_list(self, client: TradeRatedOutboxClient) -> None:
        respx.get(f"{BASE_URL}/crm-outbox").mock(return_value=httpx.Response(200, json={}))
        assert client.claim() == []

    @respx.mock
    def test_claim_raises_on_http_error(self, client: TradeRatedOutboxClient) -> None:
        respx.get(f"{BASE_URL}/crm-outbox").mock(return_value=httpx.Response(401, text="nope"))
        with pytest.raises(httpx.HTTPStatusError):
            client.claim()


class TestReportResult:
    @respx.mock
    def test_report_success_includes_st_id(self, client: TradeRatedOutboxClient) -> None:
        route = respx.post(f"{BASE_URL}/crm-outbox/1/result").mock(
            return_value=httpx.Response(200, json={})
        )
        client.report_result("1", status="succeeded", st_id="st-123")
        body = route.calls.last.request.content
        assert json.loads(body) == {"status": "succeeded", "st_id": "st-123"}

    @respx.mock
    def test_report_failure_includes_error(self, client: TradeRatedOutboxClient) -> None:
        route = respx.post(f"{BASE_URL}/crm-outbox/1/result").mock(
            return_value=httpx.Response(200, json={})
        )
        client.report_result("1", status="failed", error="boom")
        body = json.loads(route.calls.last.request.content)
        assert body == {"status": "failed", "error": "boom"}

    @respx.mock
    def test_report_raises_on_http_error(self, client: TradeRatedOutboxClient) -> None:
        respx.post(f"{BASE_URL}/crm-outbox/1/result").mock(
            return_value=httpx.Response(500, text="server error")
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.report_result("1", status="succeeded")

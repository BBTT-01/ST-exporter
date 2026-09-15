"""Tests for TrueQuote's booking lane — its own paths, envelope and vocabulary.

Every wire detail asserted here is transcribed from TrueQuote's own receiving
code on `feat/servicetitan-hosted` (`apps/admin/app/api/outbox/booking/*`,
`lib/integrations/booking-result-report.ts`), which is committed but not yet
merged to their `main`. See KNOWN_UNVERIFIED.md.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_cli.config import Settings
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.truequote import (
    TrueQuoteBookingOutboxClient,
    build_booking_body,
    perform_booking,
)
from tests.st_exporter.conftest import mock_auth_token

BASE = "https://truequote.test/api/outbox"
TOKEN = "tqm_booking_scope"


def _client() -> TrueQuoteBookingOutboxClient:
    return TrueQuoteBookingOutboxClient(BASE, TOKEN)


class TestClaim:
    @respx.mock
    def test_claims_with_a_json_body_at_their_path(self) -> None:
        route = respx.post(f"{BASE}/booking/claim").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        client = _client()
        try:
            assert client.claim(limit=7) == []
        finally:
            client.close()

        assert route.called
        # A JSON BODY, not a query string: `readLimit` reads `request.json()`,
        # so a limit in the query would be silently ignored, not rejected.
        assert json.loads(route.calls.last.request.content) == {"limit": 7}
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"

    @respx.mock
    def test_reads_their_field_names_not_traderateds(self) -> None:
        """`item_id` not `id`, `booking` not `payload`, and no `kind` at all."""
        respx.post(f"{BASE}/booking/claim").mock(
            return_value=httpx.Response(
                200,
                json={
                    "lease_seconds": 300,
                    "items": [
                        {
                            "item_id": "8b1b0f9e-0000-4000-8000-000000000000",
                            "idempotency_key": "servicetitan:booking:sess-1",
                            "tenant_id": "999",
                            "booking_provider_id": "77",
                            "booking": {"sessionId": "sess-1", "name": "Jane"},
                            "attempt": 1,
                            "max_attempts": 5,
                        }
                    ],
                },
            )
        )
        client = _client()
        try:
            items = client.claim()
        finally:
            client.close()

        assert len(items) == 1
        assert items[0].id == "8b1b0f9e-0000-4000-8000-000000000000"
        assert items[0].idempotency_key == "servicetitan:booking:sess-1"
        assert items[0].kind == "booking"
        assert items[0].payload == {"sessionId": "sess-1", "name": "Jane"}
        assert items[0].extra["booking_provider_id"] == "77"

    @respx.mock
    def test_an_item_with_no_identity_is_dropped_not_collapsed(self, caplog) -> None:
        """A blank `idempotency_key` is a LOST booking, not a harmless one.

        The reviewer's repro: two raw items spelling their id `id` instead of
        `item_id` both parse to `idempotency_key=""`. In the drain the first is
        performed and ledgered under `("truequote", "")`; the second then looks
        like an idempotency replay and is reported *succeeded, with the first
        customer's booking id*. That customer's booking is never created and
        TrueQuote is told it was delivered.

        The Profit Wizard lane already refuses such a row; this asserts the two
        lanes now agree.
        """
        respx.post(f"{BASE}/booking/claim").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "row-A", "booking": {"name": "Alice"}, "booking_provider_id": 7},
                        {"id": "row-B", "booking": {"name": "Bob"}, "booking_provider_id": 7},
                        {
                            "item_id": "row-C",
                            "idempotency_key": "servicetitan:booking:sess-C",
                            "booking": {"name": "Carol"},
                            "booking_provider_id": 7,
                        },
                    ]
                },
            )
        )
        client = _client()
        try:
            with caplog.at_level(logging.WARNING):
                items = client.claim()
        finally:
            client.close()

        assert [i.id for i in items] == ["row-C"], "blank-keyed items must not reach the drain"
        assert sum("dropping a claimed item" in r.getMessage() for r in caplog.records) == 2

    @respx.mock
    def test_an_item_with_a_key_but_no_id_is_dropped_too(self) -> None:
        """`item.id` is what the result is POSTed against, so a blank one
        settles nothing in their queue even when the write landed."""
        respx.post(f"{BASE}/booking/claim").mock(
            return_value=httpx.Response(
                200,
                json={"items": [{"idempotency_key": "k", "booking": {"name": "Dan"}}]},
            )
        )
        client = _client()
        try:
            assert client.claim() == []
        finally:
            client.close()

    def test_the_blank_key_collapse_cannot_happen_through_the_drain(self) -> None:
        """End to end over the real drain loop: the repro's two raw items yield
        no booking at all rather than one booking reported twice."""
        from unittest.mock import MagicMock

        from st_exporter.outbox.client import drop_unidentified
        from st_exporter.outbox.drain import drain_outbox
        from st_exporter.outbox.ledger import OutboxLedger
        from st_exporter.outbox.truequote import _to_item
        from st_exporter.sheets import InMemorySheetsStore

        raw = [
            {"id": "row-A", "booking": {"name": "Alice"}, "booking_provider_id": 7},
            {"id": "row-B", "booking": {"name": "Bob"}, "booking_provider_id": 7},
        ]
        items = drop_unidentified([_to_item(r) for r in raw], "truequote")

        performs: list[str] = []
        lane = MagicMock()
        lane.product = "truequote"
        lane.claim.return_value = items
        lane.perform.side_effect = lambda c, i: (
            performs.append(i.payload["name"]),
            f"st-{len(performs)}",
        )[1]

        summary = drain_outbox(MagicMock(), lane, OutboxLedger(InMemorySheetsStore()))

        assert performs == []
        assert summary.replayed == 0
        lane.report_success.assert_not_called()


class TestReport:
    @respx.mock
    def test_a_success_names_the_booking_id_not_st_id(self) -> None:
        """THE reason there is no shared result vocabulary: TrueQuote reads
        `booking_id`/`servicetitan_booking_id`/`external_id` and does NOT read
        `st_id`. A shared shape would silently lose the booking id and the lead
        would never learn it was booked."""
        route = respx.post(f"{BASE}/booking/result").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        item = OutboxItem(id="item-1", idempotency_key="k-1", kind="booking", payload={})
        client = _client()
        try:
            client.report_success(item, "555")
        finally:
            client.close()

        body = json.loads(route.calls.last.request.content)
        assert body["booking_id"] == "555"
        assert "st_id" not in body
        # Both identifiers: either alone is enough for them to find the row, and
        # sending both survives one of them being dropped.
        assert body["item_id"] == "item-1"
        assert body["idempotency_key"] == "k-1"
        # `succeeded` is in their SUCCESS_WORDS set (booking-result-report.ts:16).
        assert body["status"] == "succeeded"

    @respx.mock
    def test_a_failure_carries_the_error_text(self) -> None:
        route = respx.post(f"{BASE}/booking/result").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        item = OutboxItem(id="item-2", idempotency_key="k-2", kind="booking", payload={})
        client = _client()
        try:
            client.report_failure(item, "ServiceTitan 404")
        finally:
            client.close()

        body = json.loads(route.calls.last.request.content)
        assert body["status"] == "failed"  # in their FAILURE_WORDS set
        assert body["error"] == "ServiceTitan 404"


class TestBookingBody:
    """`item.payload` is TrueQuote's OWN input shape, not a ServiceTitan body —
    the direct path applies `createBookingPayload` after the queue, and for a
    Hosted company that has to happen here instead."""

    def test_builds_the_shape_servicetitan_requires(self) -> None:
        body = build_booking_body(
            {
                "sessionId": "sess-1",
                "name": "Jane Doe",
                "phone": "555-0100",
                "email": "jane@example.com",
                "summary": "Quote request: two doors",
                "address": {"street": "1 Main St", "city": "Denver", "zip": "80202"},
            }
        )
        assert body["source"] == "TrueQuote"
        assert body["externalId"] == "sess-1"
        assert body["name"] == "Jane Doe"
        assert body["summary"] == "Quote request: two doors"
        assert body["isFirstTimeClient"] is True
        # ServiceTitan IGNORES flat phone/email fields — contacts must be an
        # array, or every booking arrives with no way to reach the homeowner.
        assert body["contacts"] == [
            {"type": "Phone", "value": "555-0100"},
            {"type": "Email", "value": "jane@example.com"},
        ]
        assert body["address"] == {
            "street": "1 Main St",
            "city": "Denver",
            "zip": "80202",
            "country": "USA",
        }

    def test_no_contact_details_means_no_contacts_key(self) -> None:
        body = build_booking_body({"sessionId": "s", "summary": "x"})
        assert "contacts" not in body
        assert "address" not in body

    def test_a_missing_name_falls_back_rather_than_posting_a_blank(self) -> None:
        assert build_booking_body({"summary": "x"})["name"] == "TrueQuote website lead"

    def test_a_missing_summary_is_rebuilt_because_servicetitan_rejects_a_blank(self) -> None:
        body = build_booking_body(
            {
                "trade": "garage_door",
                "minPrice": 2460,
                "maxPrice": 2718,
                "quoteSummary": ["16x7", "insulated"],
                "renderUrl": "https://img.test/a.png",
                "notes": "call after 5",
            }
        )
        assert body["summary"] == (
            "Quote request: garage_door: range $2460-2718. "
            "Configuration: 16x7, insulated. Door preview: https://img.test/a.png. "
            "Notes: call after 5"
        )


class TestPerformBooking:
    @respx.mock
    def test_posts_to_the_booking_provider_route_and_returns_the_id(
        self, st_settings: Settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        route = respx.post(
            f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}"
            "/booking-provider/77/bookings"
        ).mock(return_value=httpx.Response(200, json={"id": 90210}))

        item = OutboxItem(
            id="i",
            idempotency_key="k",
            kind="booking",
            payload={"sessionId": "sess-1", "summary": "x"},
            extra={"booking_provider_id": "77"},
        )
        client = ServiceTitanClient(st_settings)
        try:
            assert perform_booking(client, item) == "90210"
        finally:
            client.close()
        assert route.called

    def test_an_item_with_no_provider_id_fails_rather_than_guessing(
        self, st_settings: Settings
    ) -> None:
        item = OutboxItem(id="i", idempotency_key="k", kind="booking", payload={}, extra={})
        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(ValueError, match="booking_provider_id"):
                perform_booking(client, item)
        finally:
            client.close()

    @respx.mock
    def test_the_queued_tenant_id_never_redirects_the_write(self, st_settings: Settings) -> None:
        """A tenant id in TrueQuote's database must not be able to steer a write
        into somebody else's ServiceTitan account. The runner has exactly one
        tenant — its own ST_TENANT_ID."""
        mock_auth_token(st_settings.auth_url)
        route = respx.post(
            f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}"
            "/booking-provider/77/bookings"
        ).mock(return_value=httpx.Response(200, json={"id": 1}))

        raw = {
            "item_id": "i",
            "idempotency_key": "k",
            "tenant_id": "666666",
            "booking_provider_id": "77",
            "booking": {"sessionId": "s", "summary": "x"},
        }
        respx.post(f"{BASE}/booking/claim").mock(
            return_value=httpx.Response(200, json={"items": [raw]})
        )
        outbox = _client()
        try:
            item = outbox.claim()[0]
        finally:
            outbox.close()

        client = ServiceTitanClient(st_settings)
        try:
            perform_booking(client, item)
        finally:
            client.close()

        assert route.called
        assert "666666" not in str(route.calls.last.request.url)

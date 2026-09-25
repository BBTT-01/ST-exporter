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
from st_exporter.outbox.booking_provider import (
    BOOKING_PROVIDER_TAGS_PERMISSION,
    BookingProviderTagError,
    TrueQuoteBookingProvider,
)
from st_exporter.outbox.booking_schedule import TrueQuoteBookingSchedule, TrueQuoteBusinessUnit
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.lanes import TrueQuoteLane
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


def _crm(settings: Settings) -> str:
    return f"{settings.api_base}/crm/v2/tenant/{settings.tenant_id}"


def _tags_route(settings: Settings, tags: list[dict], has_more: bool = False) -> respx.Route:
    return respx.get(f"{_crm(settings)}/booking-provider-tags").mock(
        return_value=httpx.Response(200, json={"data": tags, "hasMore": has_more})
    )


def _perform(
    client: ServiceTitanClient, item: OutboxItem, provider: TrueQuoteBookingProvider
) -> str:
    return perform_booking(
        client, item, provider, TrueQuoteBusinessUnit(client, "7"), TrueQuoteBookingSchedule()
    )


def _hosted_item(n: int) -> OutboxItem:
    return OutboxItem(
        id=f"i-{n}",
        idempotency_key=f"k-{n}",
        kind="booking",
        payload={"sessionId": f"s-{n}", "summary": "x"},
        extra={"booking_provider_id": None},
    )


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
            assert _perform(client, item, TrueQuoteBookingProvider(client)) == "90210"
        finally:
            client.close()
        assert route.called

    @respx.mock
    def test_an_item_with_no_provider_id_posts_under_the_runners_tag(
        self, st_settings: Settings
    ) -> None:
        """A Hosted company sends no provider id: the runner's own `TrueQuote`
        tag is resolved and the booking is filed under it."""
        mock_auth_token(st_settings.auth_url)
        _tags_route(st_settings, [{"id": 501, "tagName": "TrueQuote", "active": True}])
        route = respx.post(f"{_crm(st_settings)}/booking-provider/501/bookings").mock(
            return_value=httpx.Response(200, json={"id": 4})
        )

        item = OutboxItem(
            id="i",
            idempotency_key="k",
            kind="booking",
            payload={"summary": "x"},
            extra={"booking_provider_id": None},
        )
        client = ServiceTitanClient(st_settings)
        try:
            assert _perform(client, item, TrueQuoteBookingProvider(client)) == "4"
        finally:
            client.close()
        assert route.called

    @respx.mock
    def test_an_item_that_carries_a_provider_id_never_touches_the_tags(
        self, st_settings: Settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        tags = _tags_route(st_settings, [])
        respx.post(f"{_crm(st_settings)}/booking-provider/77/bookings").mock(
            return_value=httpx.Response(200, json={"id": 1})
        )
        item = OutboxItem(
            id="i",
            idempotency_key="k",
            kind="booking",
            payload={"summary": "x"},
            extra={"booking_provider_id": "77"},
        )
        client = ServiceTitanClient(st_settings)
        try:
            _perform(client, item, TrueQuoteBookingProvider(client))
        finally:
            client.close()
        assert not tags.called

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
            _perform(client, item, TrueQuoteBookingProvider(client))
        finally:
            client.close()

        assert route.called
        assert "666666" not in str(route.calls.last.request.url)


class TestBookingProviderTag:
    """The runner owns the `TrueQuote` Booking Provider Tag: found, or created
    once, and never duplicated."""

    @respx.mock
    def test_an_existing_tag_is_reused_across_pages_and_never_recreated(
        self, st_settings: Settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        tags = respx.get(f"{_crm(st_settings)}/booking-provider-tags").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"data": [{"id": 10, "tagName": "Website"}], "hasMore": True},
                ),
                httpx.Response(
                    200,
                    json={"data": [{"id": 105684521, "tagName": "truequote"}], "hasMore": False},
                ),
            ]
        )
        create = respx.post(f"{_crm(st_settings)}/booking-provider-tags").mock(
            return_value=httpx.Response(200, json={"id": 999})
        )

        client = ServiceTitanClient(st_settings)
        try:
            provider = TrueQuoteBookingProvider(client)
            assert provider.tag_id() == 105684521
            assert provider.tag_id() == 105684521
        finally:
            client.close()

        assert tags.call_count == 2, "both pages read once, then cached for the run"
        assert [c.request.url.params["page"] for c in tags.calls] == ["1", "2"]
        assert not create.called

    @respx.mock
    def test_a_missing_tag_is_created_once_for_a_whole_batch(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        tags = _tags_route(st_settings, [{"id": 10, "tagName": "Website", "active": True}])
        create = respx.post(f"{_crm(st_settings)}/booking-provider-tags").mock(
            return_value=httpx.Response(200, json={"id": 777, "tagName": "TrueQuote"})
        )
        bookings = respx.post(f"{_crm(st_settings)}/booking-provider/777/bookings").mock(
            return_value=httpx.Response(200, json={"id": 1})
        )

        client = ServiceTitanClient(st_settings)
        try:
            lane = TrueQuoteLane(
                _client(), TrueQuoteBookingProvider(client), TrueQuoteBusinessUnit(client, "7")
            )
            for n in range(3):
                lane.perform(client, _hosted_item(n))
            lane.close()
        finally:
            client.close()

        assert tags.call_count == 1
        assert create.call_count == 1
        assert json.loads(create.calls.last.request.content)["tagName"] == "TrueQuote"
        assert bookings.call_count == 3

    @respx.mock
    def test_a_name_with_stray_whitespace_still_matches(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _tags_route(st_settings, [{"id": 42, "tagName": "  TrueQuote ", "active": True}])
        create = respx.post(f"{_crm(st_settings)}/booking-provider-tags").mock(
            return_value=httpx.Response(200, json={"id": 999})
        )
        client = ServiceTitanClient(st_settings)
        try:
            assert TrueQuoteBookingProvider(client).tag_id() == 42
        finally:
            client.close()
        assert not create.called

    @respx.mock
    def test_the_list_sends_no_active_filter_so_inactive_tags_are_seen(
        self, st_settings: Settings
    ) -> None:
        """BookingProviderTags_GetList documents no `active` parameter, so none is
        sent; the inactive tag in the answer is refused, not shadowed by a new one."""
        mock_auth_token(st_settings.auth_url)
        tags = _tags_route(
            st_settings,
            [
                {"id": 10, "tagName": "Website", "active": True},
                {"id": 55, "tagName": "TrueQuote", "active": False},
            ],
        )
        create = respx.post(f"{_crm(st_settings)}/booking-provider-tags").mock(
            return_value=httpx.Response(200, json={"id": 999})
        )
        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(BookingProviderTagError, match="id 55"):
                TrueQuoteBookingProvider(client).tag_id()
        finally:
            client.close()
        assert "active" not in tags.calls.last.request.url.params
        assert not create.called

    @respx.mock
    def test_after_a_403_an_item_with_its_own_provider_id_still_posts(
        self, st_settings: Settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(f"{_crm(st_settings)}/booking-provider-tags").mock(
            return_value=httpx.Response(403, json={"title": "Scope validation failed"})
        )
        direct = respx.post(f"{_crm(st_settings)}/booking-provider/77/bookings").mock(
            return_value=httpx.Response(200, json={"id": 5})
        )
        carried = OutboxItem(
            id="i-direct",
            idempotency_key="k-direct",
            kind="booking",
            payload={"summary": "x"},
            extra={"booking_provider_id": "77"},
        )
        client = ServiceTitanClient(st_settings)
        try:
            provider = TrueQuoteBookingProvider(client)
            with pytest.raises(BookingProviderTagError):
                _perform(client, _hosted_item(1), provider)
            assert _perform(client, carried, provider) == "5"
        finally:
            client.close()
        assert direct.call_count == 1

    @respx.mock
    def test_an_inactive_tag_is_refused_rather_than_duplicated(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _tags_route(st_settings, [{"id": 55, "tagName": "TrueQuote", "active": False}])
        create = respx.post(f"{_crm(st_settings)}/booking-provider-tags").mock(
            return_value=httpx.Response(200, json={"id": 999})
        )

        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(BookingProviderTagError, match="inactive"):
                TrueQuoteBookingProvider(client).tag_id()
        finally:
            client.close()
        assert not create.called

    @respx.mock
    def test_a_403_names_the_missing_permission_and_is_asked_only_once(
        self, st_settings: Settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        tags = respx.get(f"{_crm(st_settings)}/booking-provider-tags").mock(
            return_value=httpx.Response(
                403,
                json={
                    "title": "Scope validation failed. Access token does not have permission "
                    "to access 'GET /tenant/{tenant}/booking-provider-tags'"
                },
            )
        )
        bookings = respx.post(url__regex=r".*/booking-provider/.+/bookings").mock(
            return_value=httpx.Response(200, json={"id": 1})
        )

        client = ServiceTitanClient(st_settings)
        try:
            provider = TrueQuoteBookingProvider(client)
            for n in range(2):
                with pytest.raises(BookingProviderTagError) as caught:
                    _perform(client, _hosted_item(n), provider)
                assert BOOKING_PROVIDER_TAGS_PERMISSION in str(caught.value)
                assert "403" in str(caught.value)
        finally:
            client.close()

        assert BOOKING_PROVIDER_TAGS_PERMISSION == "CRM -> Booking Provider Tags (Read + Write)"
        assert tags.call_count == 1
        assert not bookings.called

    @respx.mock
    def test_a_403_on_create_names_the_permission_too(self, st_settings: Settings) -> None:
        """Read granted, Write not: the list works, the create is refused."""
        mock_auth_token(st_settings.auth_url)
        _tags_route(st_settings, [])
        respx.post(f"{_crm(st_settings)}/booking-provider-tags").mock(
            return_value=httpx.Response(403, json={"title": "Scope validation failed"})
        )

        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(BookingProviderTagError, match="create") as caught:
                TrueQuoteBookingProvider(client).tag_id()
        finally:
            client.close()
        assert BOOKING_PROVIDER_TAGS_PERMISSION in str(caught.value)

    @respx.mock
    def test_a_non_403_failure_is_not_blamed_on_the_permission(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(f"{_crm(st_settings)}/booking-provider-tags").mock(
            return_value=httpx.Response(404, json={"title": "nope"})
        )

        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(BookingProviderTagError) as caught:
                TrueQuoteBookingProvider(client).tag_id()
        finally:
            client.close()
        assert BOOKING_PROVIDER_TAGS_PERMISSION not in str(caught.value)

    @respx.mock
    def test_the_tag_list_is_logged_as_evidence(self, st_settings: Settings, caplog) -> None:
        """Whether the booking provider id IS the tag id is unconfirmed live; the
        first resolution's log line is what settles it."""
        mock_auth_token(st_settings.auth_url)
        _tags_route(
            st_settings,
            [{"id": 10, "tagName": "Website"}, {"id": 105684521, "tagName": "TrueQuote"}],
        )
        client = ServiceTitanClient(st_settings)
        try:
            with caplog.at_level(logging.INFO, logger="st_exporter"):
                TrueQuoteBookingProvider(client).tag_id()
        finally:
            client.close()
        assert any(
            "10='Website'" in r.getMessage() and "105684521='TrueQuote'" in r.getMessage()
            for r in caplog.records
        )

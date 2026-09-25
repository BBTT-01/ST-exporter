"""TrueQuote bookings arrive schedulable: a requested ``start`` and a ``businessUnitId``."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_cli.config import Settings
from st_exporter.outbox.booking_provider import TrueQuoteBookingProvider
from st_exporter.outbox.booking_schedule import (
    BUSINESS_UNITS_PERMISSION,
    BookingScheduleError,
    TrueQuoteBookingSchedule,
    TrueQuoteBusinessUnit,
    booking_settings,
)
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.lanes import TrueQuoteLane
from st_exporter.outbox.routes import TRUEQUOTE_ROUTES
from st_exporter.outbox.settings import LaneCredentials
from st_exporter.outbox.truequote import perform_booking
from tests.st_exporter.conftest import mock_auth_token

# Wednesday 2026-09-23 15:00 UTC = 11:00 in New York (EDT, -04:00).
WEDNESDAY = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)


def _units_url(settings: Settings) -> str:
    return f"{settings.api_base}/settings/v2/tenant/{settings.tenant_id}/business-units"


def _units_route(settings: Settings, units: list[dict]) -> respx.Route:
    return respx.get(_units_url(settings)).mock(
        return_value=httpx.Response(200, json={"data": units, "hasMore": False})
    )


def _bookings_route(settings: Settings) -> respx.Route:
    return respx.post(
        f"{settings.api_base}/crm/v2/tenant/{settings.tenant_id}/booking-provider/77/bookings"
    ).mock(return_value=httpx.Response(200, json={"id": 1}))


def _item(n: int = 1, **payload: object) -> OutboxItem:
    return OutboxItem(
        id=f"i-{n}",
        idempotency_key=f"k-{n}",
        kind="booking",
        payload={"sessionId": f"s-{n}", "name": "Pat", "summary": "Two doors", **payload},
        extra={"booking_provider_id": "77"},
    )


def _start(payload: dict[str, object], now: datetime = WEDNESDAY, tz: str | None = None) -> str:
    return TrueQuoteBookingSchedule(tz).start(payload, now)


class TestStart:
    def test_the_fallback_is_the_next_business_morning_in_new_york(self) -> None:
        assert _start({}) == "2026-09-24T09:00:00-04:00"

    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            (datetime(2026, 9, 25, 15, tzinfo=timezone.utc), "2026-09-28T09:00:00-04:00"),  # Fri
            (datetime(2026, 9, 26, 15, tzinfo=timezone.utc), "2026-09-28T09:00:00-04:00"),  # Sat
            (datetime(2026, 9, 27, 15, tzinfo=timezone.utc), "2026-09-28T09:00:00-04:00"),  # Sun
            # Saturday 02:00 UTC is still Friday evening in New York.
            (datetime(2026, 9, 26, 2, tzinfo=timezone.utc), "2026-09-28T09:00:00-04:00"),
        ],
    )
    def test_the_fallback_skips_weekends(self, now: datetime, expected: str) -> None:
        assert _start({}, now) == expected

    def test_the_fallback_carries_the_winter_offset(self) -> None:
        assert _start({}, datetime(2026, 12, 2, 15, tzinfo=timezone.utc)) == (
            "2026-12-03T09:00:00-05:00"
        )

    def test_the_timezone_setting_moves_the_morning(self) -> None:
        assert _start({}, tz="America/Los_Angeles") == "2026-09-24T09:00:00-07:00"

    def test_an_unknown_timezone_is_refused_by_name(self) -> None:
        with pytest.raises(BookingScheduleError, match="TRUEQUOTE_BOOKING_TIMEZONE"):
            _start({}, tz="Mars/Olympus")

    @pytest.mark.parametrize(
        ("preferred", "expected"),
        [
            ("2026-09-29T14:30", "2026-09-29T14:30:00-04:00"),
            ("2026-09-29T18:30:00Z", "2026-09-29T14:30:00-04:00"),
            ("2026-09-29T11:30:00-07:00", "2026-09-29T14:30:00-04:00"),
            ("2026-09-26", "2026-09-26T09:00:00-04:00"),
        ],
    )
    def test_an_iso_preferred_time_is_honoured_in_tenant_time(
        self, preferred: str, expected: str
    ) -> None:
        assert _start({"preferredTime": preferred}) == expected

    @pytest.mark.parametrize("preferred", ["Tomorrow afternoon", "", "2026-09-20T10:00", 5])
    def test_free_text_or_a_past_preferred_time_falls_back(self, preferred: object) -> None:
        assert _start({"preferredTime": preferred}) == "2026-09-24T09:00:00-04:00"


class TestBusinessUnit:
    @respx.mock
    def test_the_lowest_active_unit_is_used(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _units_route(
            st_settings,
            [
                {"id": 3, "name": "Closed", "active": False},
                {"id": 40, "name": "Commercial", "active": True},
                {"id": 12, "name": "Residential", "active": True},
            ],
        )
        client = ServiceTitanClient(st_settings)
        try:
            assert TrueQuoteBusinessUnit(client).unit_id() == 12
        finally:
            client.close()

    @respx.mock
    def test_no_active_unit_is_an_error_not_a_booking_without_one(
        self, st_settings: Settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _units_route(st_settings, [{"id": 3, "active": False}])
        bookings = _bookings_route(st_settings)
        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(BookingScheduleError, match="no active business unit"):
                perform_booking(
                    client,
                    _item(),
                    TrueQuoteBookingProvider(client),
                    TrueQuoteBusinessUnit(client),
                    TrueQuoteBookingSchedule(),
                )
        finally:
            client.close()
        assert not bookings.called

    @respx.mock
    def test_the_override_secret_wins_and_skips_the_lookup(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        units = _units_route(st_settings, [{"id": 12, "active": True}])
        client = ServiceTitanClient(st_settings)
        try:
            assert TrueQuoteBusinessUnit(client, " 555 ").unit_id() == 555
        finally:
            client.close()
        assert not units.called

    @pytest.mark.parametrize("override", ["abc", "0", "-4", "12.5"])
    def test_a_malformed_override_is_refused_by_name(self, override: str) -> None:
        with pytest.raises(BookingScheduleError, match="TRUEQUOTE_BUSINESS_UNIT_ID"):
            TrueQuoteBusinessUnit(None, override).unit_id()  # type: ignore[arg-type]

    @respx.mock
    def test_a_403_names_the_permission_is_asked_once_and_posts_nothing(
        self, st_settings: Settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        units = respx.get(_units_url(st_settings)).mock(
            return_value=httpx.Response(403, json={"title": "Scope validation failed"})
        )
        tags = respx.get(url__regex=r".*/booking-provider-tags")
        bookings = respx.post(url__regex=r".*/bookings")
        client = ServiceTitanClient(st_settings)
        try:
            business_unit = TrueQuoteBusinessUnit(client)
            provider = TrueQuoteBookingProvider(client)
            for n in range(2):
                hosted = _item(n)
                hosted.extra["booking_provider_id"] = None
                with pytest.raises(BookingScheduleError) as caught:
                    perform_booking(
                        client, hosted, provider, business_unit, TrueQuoteBookingSchedule()
                    )
                assert BUSINESS_UNITS_PERMISSION in str(caught.value)
                assert "403" in str(caught.value)
        finally:
            client.close()

        assert BUSINESS_UNITS_PERMISSION == "Settings -> Business Units (Read)"
        assert units.call_count == 1
        assert not tags.called
        assert not bookings.called


class TestBookingBody:
    @respx.mock
    def test_the_booking_carries_start_and_business_unit_and_nothing_else_moves(
        self, st_settings: Settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _units_route(st_settings, [{"id": 12, "active": True}])
        bookings = _bookings_route(st_settings)
        client = ServiceTitanClient(st_settings)
        try:
            perform_booking(
                client,
                _item(phone="555-0100"),
                TrueQuoteBookingProvider(client),
                TrueQuoteBusinessUnit(client),
                TrueQuoteBookingSchedule(),
                now=WEDNESDAY,
            )
        finally:
            client.close()

        assert json.loads(bookings.calls.last.request.content) == {
            "source": "TrueQuote",
            "externalId": "s-1",
            "name": "Pat",
            "summary": "Two doors",
            "isFirstTimeClient": True,
            "contacts": [{"type": "Phone", "value": "555-0100"}],
            "start": "2026-09-24T09:00:00-04:00",
            "businessUnitId": 12,
        }

    @respx.mock
    def test_the_lane_resolves_the_unit_once_per_run_from_its_secrets(
        self, st_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRUEQUOTE_BOOKING_TIMEZONE", "America/Chicago")
        mock_auth_token(st_settings.auth_url)
        units = _units_route(st_settings, [{"id": 12, "active": True}])
        bookings = _bookings_route(st_settings)
        client = ServiceTitanClient(st_settings)
        credentials = LaneCredentials(
            product="truequote",
            base_url="https://tq.test/api/outbox",
            machine_token="t",
            routes=TRUEQUOTE_ROUTES,
        )
        lane = TrueQuoteLane.build(credentials, client)
        try:
            for n in range(3):
                lane.perform(client, _item(n, preferredTime="2099-01-05T10:00"))
        finally:
            lane.close()
            client.close()

        assert units.call_count == 1
        posted = [json.loads(call.request.content) for call in bookings.calls]
        assert [body["businessUnitId"] for body in posted] == [12, 12, 12]
        assert {body["start"] for body in posted} == {"2099-01-05T10:00:00-06:00"}


def test_the_settings_are_read_from_the_environment_and_blank_is_unset() -> None:
    assert booking_settings({}) == (None, None)
    assert booking_settings(
        {"TRUEQUOTE_BUSINESS_UNIT_ID": "", "TRUEQUOTE_BOOKING_TIMEZONE": "America/Denver"}
    ) == (None, "America/Denver")

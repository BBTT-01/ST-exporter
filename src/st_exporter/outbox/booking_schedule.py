"""What makes a TrueQuote booking schedulable: a requested start and a business unit.

Booking 115584114 (2026-09-25) reached ServiceTitan with ``start`` 0001-01-01 and
no ``businessUnitId``, so a dispatcher had nothing to schedule against and it was
dismissed within minutes. Direct mode (``createBookingPayload``, TrueQuote
``packages/servicetitan/src/server.ts``) sends neither field either; the runner
adds them because it is the one side that can read the contractor's tenant.

- ``start`` is the customer's ``preferredTime`` when it is an ISO 8601 date or
  datetime still in the future, otherwise the next business day (Mon-Fri) at
  09:00, in ``TRUEQUOTE_BOOKING_TIMEZONE`` (default America/New_York). The
  exporter knows no tenant time zone: ServiceTitan's business-unit record carries
  none, so the zone is a setting.
- ``businessUnitId`` is ``TRUEQUOTE_BUSINESS_UNIT_ID`` when set, otherwise the
  lowest-id active unit from ``settings/v2/tenant/{t}/business-units``, resolved
  once per run and cached, a failure included.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from st_cli.client import ServiceTitanClient
from st_cli.pagination import fetch_all
from st_exporter.logging_setup import announce_to_actions, logger
from st_exporter.scopes import is_permission_denied

BUSINESS_UNIT_ENV = "TRUEQUOTE_BUSINESS_UNIT_ID"
TIMEZONE_ENV = "TRUEQUOTE_BOOKING_TIMEZONE"
DEFAULT_TIMEZONE = "America/New_York"

#: The ServiceTitan tick-box this module needs, in the words of the app registration screen.
BUSINESS_UNITS_PERMISSION = "Settings -> Business Units (Read)"

#: The payload field a customer-preferred slot is read from. TrueQuote already carries it
#: (``ServiceTitanLeadInput.preferredTime``) as free text; only an ISO 8601 value is honoured.
PREFERRED_START_FIELD = "preferredTime"

_DEFAULT_START = time(9, 0)
_PAGE_SIZE = 200


class BookingScheduleError(Exception):
    """A TrueQuote booking cannot be given a start or a business unit."""


class TrueQuoteBusinessUnit:
    """Lazily resolves the business unit every TrueQuote booking is filed under."""

    def __init__(self, client: ServiceTitanClient, override: str | None = None) -> None:
        self._client = client
        self._override = (override or "").strip()
        self._id: int | None = None
        self._error: BookingScheduleError | None = None

    def unit_id(self) -> int:
        """The business unit id; a failure is cached as well as a success."""
        if self._error is not None:
            raise self._error
        if self._id is None:
            try:
                self._id = self._from_override() if self._override else self._find()
            except BookingScheduleError as exc:
                self._error = exc
                raise
        return self._id

    def _from_override(self) -> int:
        if not self._override.isdigit() or int(self._override) == 0:
            raise BookingScheduleError(
                f"{BUSINESS_UNIT_ENV} must be a ServiceTitan business unit id (a positive "
                f"whole number), got {self._override!r}; no TrueQuote booking was filed"
            )
        logger.info(
            "TrueQuote bookings use business unit %s from %s", self._override, BUSINESS_UNIT_ENV
        )
        return int(self._override)

    def _find(self) -> int:
        try:
            units = list(
                fetch_all(self._client, "settings", "business-units", page_size=_PAGE_SIZE)
            )
        except Exception as exc:
            raise _describe(exc) from exc

        logger.info(
            "business units in this tenant: %s",
            ", ".join(_describe_unit(unit) for unit in units) or "(none)",
        )
        active = sorted(
            int(unit["id"]) for unit in units if "id" in unit and unit.get("active", True)
        )
        if not active:
            raise BookingScheduleError(
                "this ServiceTitan tenant has no active business unit, so no TrueQuote booking "
                f"can be filed. Activate one, or set {BUSINESS_UNIT_ENV} to the unit's id"
            )
        logger.info("TrueQuote bookings use business unit %s (lowest active id)", active[0])
        return active[0]


def _describe_unit(unit: dict[str, Any]) -> str:
    state = "" if unit.get("active", True) else " (inactive)"
    return f"{unit.get('id')}={unit.get('name')!r}{state}"


def _describe(exc: Exception) -> BookingScheduleError:
    if not is_permission_denied(exc):
        return BookingScheduleError(
            f"could not list business units, so no TrueQuote booking can be filed: {exc}"
        )
    message = (
        "ServiceTitan refused to list business units (HTTP 403), so no TrueQuote booking can "
        f"be filed. The contractor's ServiceTitan app is missing the {BUSINESS_UNITS_PERMISSION} "
        "permission: tick it in the ServiceTitan Developer Portal and re-authorise the app, "
        f"or set {BUSINESS_UNIT_ENV}"
    )
    announce_to_actions("TrueQuote business unit", message, level="error")
    return BookingScheduleError(message)


class TrueQuoteBookingSchedule:
    """Turns a payload into the ``start`` a booking is requested for."""

    def __init__(self, timezone_name: str | None = None) -> None:
        self._name = (timezone_name or "").strip() or DEFAULT_TIMEZONE

    def zone(self) -> tzinfo:
        try:
            return ZoneInfo(self._name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise BookingScheduleError(
                f"{TIMEZONE_ENV} is not an IANA time zone name (e.g. America/Chicago): "
                f"{self._name!r}; no TrueQuote booking was filed"
            ) from exc

    def start(self, payload: Mapping[str, Any], now: datetime) -> str:
        zone = self.zone()
        local_now = now.astimezone(zone)
        preferred = _preferred_start(payload.get(PREFERRED_START_FIELD), zone)
        chosen = (
            preferred if preferred and preferred > local_now else next_business_morning(local_now)
        )
        return chosen.isoformat(timespec="seconds")


def next_business_morning(local_now: datetime) -> datetime:
    """09:00 on the next Mon-Fri after ``local_now``'s date, in ``local_now``'s zone."""
    day = local_now.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, _DEFAULT_START, tzinfo=local_now.tzinfo)


def _preferred_start(raw: object, zone: tzinfo) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), _DEFAULT_START, tzinfo=zone)
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def booking_settings(env: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """``(business unit override, time zone name)`` from the environment; blank is unset."""
    source = os.environ if env is None else env
    return (source.get(BUSINESS_UNIT_ENV) or None, source.get(TIMEZONE_ENV) or None)

"""Fixture dispatch tenant: non-job appointments and the timesheet codes they name.

Synthetic in every value, and shaped like the read-only probe of a production
tenant on 2026-10-01 (``KNOWN_UNVERIFIED.md``): the same sixteen keys on every
appointment and no ``end`` field, ``start`` in UTC with a ``Z`` (once with
milliseconds), ``duration`` as a .NET TimeSpan string (one with a seven-digit
fraction, and ``23:59:59`` on the all-day record), and ``timesheetCodeId`` ``0``
meaning "no code".

Every ``start`` falls inside the dispatch window of 2026-09-14, the date the
run-level tests freeze, so the window tripwire stays quiet on the happy path.
"""

from __future__ import annotations

import httpx
import respx

TENANT_ID = 12345

#: A plain lunch block: no timesheet code (``0``), still on the capacity plan.
LUNCH = {
    "id": 801,
    "technicianId": 9,
    "start": "2026-09-15T17:00:00Z",
    "name": "Lunch",
    "duration": "01:00:00",
    "timesheetCodeId": 0,
    "summary": "Fixture lunch block",
    "clearDispatchBoard": False,
    "clearTechnicianView": False,
    "removeTechnicianFromCapacityPlanning": False,
    "allDay": False,
    "showOnTechnicianSchedule": True,
    "active": True,
    "createdOn": "2026-09-01T12:00:00Z",
    "modifiedOn": "2026-09-01T12:00:00Z",
    "createdById": 5,
}

#: Milliseconds on ``start`` and a seven-digit fraction on ``duration``, both
#: written verbatim; a code that resolves; off the capacity plan.
TRAINING = {
    "id": 802,
    "technicianId": 10,
    "start": "2026-09-16T14:30:00.123Z",
    "name": "Training",
    "duration": "02:10:00.5000000",
    "timesheetCodeId": 3,
    "summary": "Fixture training session",
    "clearDispatchBoard": True,
    "clearTechnicianView": False,
    "removeTechnicianFromCapacityPlanning": True,
    "allDay": False,
    "showOnTechnicianSchedule": True,
    "active": True,
    "createdOn": "2026-09-02T12:00:00Z",
    "modifiedOn": "2026-09-03T12:00:00Z",
    "createdById": 5,
}

#: All day, so ``23:59:59``; its code was RETIRED, and still resolves because
#: the codes are listed with ``active=Any``.
PTO_ALL_DAY = {
    "id": 803,
    "technicianId": 9,
    "start": "2026-09-17T00:00:00Z",
    "name": "PTO",
    "duration": "23:59:59",
    "timesheetCodeId": 4,
    "summary": None,
    "clearDispatchBoard": True,
    "clearTechnicianView": True,
    "removeTechnicianFromCapacityPlanning": True,
    "allDay": True,
    "showOnTechnicianSchedule": True,
    "active": True,
    "createdOn": "2026-09-04T12:00:00Z",
    "modifiedOn": "2026-09-04T12:00:00Z",
    "createdById": 6,
}

APPOINTMENTS = [LUNCH, TRAINING, PTO_ALL_DAY]

#: Only ``id`` and ``code`` are read. The probe's other keys (``type``,
#: ``applicableEmployeeType``, ``rateInfo``, ``createdOn``, ``modifiedOn``) are
#: left out rather than given invented values.
TIMESHEET_CODES = [
    {"id": 3, "code": "TRAIN", "description": "Fixture training time", "active": True},
    {"id": 4, "code": "PTO", "description": "Fixture paid time off", "active": False},
    {"id": 5, "code": "SHOP", "description": "Fixture shop time", "active": True},
]


def _envelope(data: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"data": data, "hasMore": False})


def register(
    api_base: str,
    *,
    appointments: list[dict] | None = None,
    timesheet_codes: list[dict] | None = None,
) -> None:
    """Register respx routes for the non-job appointments and timesheet-codes lists."""
    base = api_base.rstrip("/")
    respx.get(f"{base}/dispatch/v2/tenant/{TENANT_ID}/non-job-appointments").mock(
        return_value=_envelope(APPOINTMENTS if appointments is None else appointments)
    )
    respx.get(f"{base}/payroll/v2/tenant/{TENANT_ID}/timesheet-codes").mock(
        return_value=_envelope(TIMESHEET_CODES if timesheet_codes is None else timesheet_codes)
    )

"""First-run fixture: two jobs, one appointment in-window, one outside it.

Registers respx routes for a run against an empty raw cache (no prior cursor).
Paired with ``tenant_run2.py``, which layers deltas on top of the cursors this
module hands back.
"""

from __future__ import annotations

import httpx
import respx

TENANT_ID = 12345

JOB_1 = {
    "id": 1,
    "number": "J-1",
    "customerId": 10,
    "locationId": 20,
    "jobStatus": "Scheduled",
    "modifiedOn": "2026-05-01T00:00:00Z",
}
# Created long before the appointment below is scheduled — the ticket's headline
# case for a *different* job (JOB_1) is covered directly in test_denormalize.py;
# here JOB_2's appointment starts outside the window in run 1 and is rescheduled
# into it in run 2 (tenant_run2.py).
JOB_2 = {
    "id": 2,
    "number": "J-2",
    "customerId": 11,
    "locationId": 21,
    "jobStatus": "Scheduled",
    "modifiedOn": "2026-01-01T00:00:00Z",
}

CUSTOMER_10 = {"id": 10, "name": "Jane Doe", "phone": "555-1111", "email": "jane@example.com"}
CUSTOMER_11 = {"id": 11, "name": "Bob Smith", "phone": "555-3333", "email": "bob@example.com"}

LOCATION_20 = {
    "id": 20,
    "address": {"street": "1 Main St", "city": "Springfield", "state": "IL", "zip": "62701"},
}
LOCATION_21 = {
    "id": 21,
    "address": {"street": "2 Oak Ave", "city": "Springfield", "state": "IL", "zip": "62701"},
}

TECHNICIAN_1 = {"id": 900, "name": "Tech One", "email": "tech1@example.com", "active": True}

CUSTOMERS_CURSOR = "cust-c1"
LOCATIONS_CURSOR = "loc-c1"
JOBS_CURSOR = "jobs-c1"
APPOINTMENTS_CURSOR = "appt-c1"
ASSIGNMENTS_CURSOR = "assign-c1"


def appointment_1(today_iso: str) -> dict:
    """In-window: scheduled today."""
    return {
        "id": 100,
        "jobId": 1,
        "start": f"{today_iso}T09:00:00-05:00",
        "end": f"{today_iso}T11:00:00-05:00",
    }


def appointment_2_outside_window(far_past_iso: str) -> dict:
    """Outside the 90-day window in run 1 — must NOT appear in the jobs tab yet."""
    return {
        "id": 200,
        "jobId": 2,
        "start": f"{far_past_iso}T09:00:00-05:00",
        "end": f"{far_past_iso}T11:00:00-05:00",
    }


def _envelope(data: list[dict], cursor: str) -> httpx.Response:
    return httpx.Response(200, json={"data": data, "hasMore": False, "continueFrom": cursor})


def register(api_base: str, *, today_iso: str, far_past_iso: str) -> None:
    """Register respx routes for the first run (empty prior raw cache)."""
    respx.get(f"{api_base}/crm/v2/tenant/{TENANT_ID}/export/customers").mock(
        return_value=_envelope([CUSTOMER_10, CUSTOMER_11], CUSTOMERS_CURSOR)
    )
    respx.get(f"{api_base}/crm/v2/tenant/{TENANT_ID}/export/locations").mock(
        return_value=_envelope([LOCATION_20, LOCATION_21], LOCATIONS_CURSOR)
    )
    respx.get(f"{api_base}/jpm/v2/tenant/{TENANT_ID}/export/jobs").mock(
        return_value=_envelope([JOB_1, JOB_2], JOBS_CURSOR)
    )
    respx.get(f"{api_base}/jpm/v2/tenant/{TENANT_ID}/export/appointments").mock(
        return_value=_envelope(
            [appointment_1(today_iso), appointment_2_outside_window(far_past_iso)],
            APPOINTMENTS_CURSOR,
        )
    )
    respx.get(f"{api_base}/dispatch/v2/tenant/{TENANT_ID}/export/appointment-assignments").mock(
        return_value=_envelope([], ASSIGNMENTS_CURSOR)
    )
    respx.get(f"{api_base}/settings/v2/tenant/{TENANT_ID}/technicians").mock(
        return_value=httpx.Response(200, json={"data": [TECHNICIAN_1], "hasMore": False})
    )
    respx.get(f"{api_base}/jpm/v2/tenant/{TENANT_ID}/job-types").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": False})
    )
    respx.get(f"{api_base}/settings/v2/tenant/{TENANT_ID}/business-units").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": False})
    )

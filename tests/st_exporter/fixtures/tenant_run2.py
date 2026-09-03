"""Second-run fixture: deltas only, layered on top of ``tenant_run1``'s cursors.

Each route asserts the request carries exactly the cursor ``tenant_run1`` handed
back — if the exporter ever re-drains from scratch instead of resuming, these
routes fail loudly rather than silently returning the full run-1 dataset again.
"""

from __future__ import annotations

import httpx
import respx

from tests.st_exporter.fixtures import tenant_run1 as run1

TENANT_ID = run1.TENANT_ID

CUSTOMER_11_UPDATED = {
    "id": 11,
    "name": "Bob Smith",
    "phone": "555-9999",
    "email": "bob@example.com",
}

CUSTOMERS_CURSOR = "cust-c2"
LOCATIONS_CURSOR = "loc-c2"
JOBS_CURSOR = "jobs-c2"
APPOINTMENTS_CURSOR = "appt-c2"
ASSIGNMENTS_CURSOR = "assign-c2"


def appointment_2_rescheduled(today_iso: str) -> dict:
    """Job 2's appointment, rescheduled from outside the window (run 1) to today."""
    return {
        "id": 200,
        "jobId": 2,
        "start": f"{today_iso}T13:00:00-05:00",
        "end": f"{today_iso}T15:00:00-05:00",
    }


def _delta(data: list[dict], cursor: str, expected_prior_cursor: str):
    def side_effect(request: httpx.Request) -> httpx.Response:
        actual = request.url.params.get("from")
        assert actual == expected_prior_cursor, (
            f"expected delta fetch to resume from {expected_prior_cursor!r}, "
            f"got {actual!r} — cursor was not persisted/passed correctly"
        )
        return httpx.Response(200, json={"data": data, "hasMore": False, "continueFrom": cursor})

    return side_effect


def register(api_base: str, *, today_iso: str) -> None:
    """Register respx routes for the second run, requiring run 1's cursors."""
    respx.get(f"{api_base}/crm/v2/tenant/{TENANT_ID}/export/customers").mock(
        side_effect=_delta([CUSTOMER_11_UPDATED], CUSTOMERS_CURSOR, run1.CUSTOMERS_CURSOR)
    )
    respx.get(f"{api_base}/crm/v2/tenant/{TENANT_ID}/export/locations").mock(
        side_effect=_delta([], LOCATIONS_CURSOR, run1.LOCATIONS_CURSOR)
    )
    respx.get(f"{api_base}/jpm/v2/tenant/{TENANT_ID}/export/jobs").mock(
        side_effect=_delta([], JOBS_CURSOR, run1.JOBS_CURSOR)
    )
    respx.get(f"{api_base}/jpm/v2/tenant/{TENANT_ID}/export/appointments").mock(
        side_effect=_delta(
            [appointment_2_rescheduled(today_iso)], APPOINTMENTS_CURSOR, run1.APPOINTMENTS_CURSOR
        )
    )
    respx.get(f"{api_base}/dispatch/v2/tenant/{TENANT_ID}/export/appointment-assignments").mock(
        side_effect=_delta([], ASSIGNMENTS_CURSOR, run1.ASSIGNMENTS_CURSOR)
    )
    respx.get(f"{api_base}/settings/v2/tenant/{TENANT_ID}/technicians").mock(
        return_value=httpx.Response(200, json={"data": [run1.TECHNICIAN_1], "hasMore": False})
    )
    respx.get(f"{api_base}/jpm/v2/tenant/{TENANT_ID}/job-types").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": False})
    )
    respx.get(f"{api_base}/settings/v2/tenant/{TENANT_ID}/business-units").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": False})
    )

"""The source records each committed contract fixture is generated from.

One place names, per tab, exactly which records the fixture under
``contracts/fixtures/<contract_version>/<tab>.json`` pins. Both
``scripts/gen_contract_fixtures.py`` (which writes those files) and
``tests/st_exporter/test_contract_fixtures.py`` (which asserts the exporter still
produces them) import from here, so the generator can never drift from the check.

**Every record here is synthetic.** ``BBTT-01/ST-exporter`` is a PUBLIC repo and
the fixtures are committed to it, so no real customer name, address, phone, email
or price may ever appear — see the scrubbing rule in ``docs/export-contract.md``.
The records are nonetheless realistic in the only two respects that matter for
drift: the exact ServiceTitan field SPELLINGS (``jobNumber``, not ``number``) and
the awkward SHAPES (null vs zero, absent vs false, repeated assets).

The bulk of them are reused, not re-invented: ``tenant_pricebook``,
``tenant_financial`` and ``tenant_run1`` already hold respx tenants built around
the awkward cases, and those constants are the fixtures' raw material. Only cases
those tenants do not already carry are defined here.
"""

from __future__ import annotations

from typing import Any

from st_exporter.denormalize import build_job_rows
from st_exporter.feeds.raw_cache import RawCache
from tests.st_exporter.fixtures import tenant_financial, tenant_pricebook, tenant_run1

# Fixed dates, never "today": a fixture regenerated next March must be
# byte-identical to this one, or the suite cries drift at a calendar.
_APPOINTMENT_DAY = "2026-09-03"
_SECOND_DAY = "2026-09-04"

# --- jobs ---------------------------------------------------------------------
#
# The `jobs.v2` grain is the point of this fixture: appointment 100 carries a crew
# of two and therefore produces TWO rows sharing one `st_appointment_id`. A
# consumer that still keys on `st_appointment_id` alone (the `jobs.v1` rule) will
# fail on this fixture, which is exactly what it is for.

#: A technician assigned, unassigned, then assigned again — only the LATEST event
#: counts, so they ARE on the crew. Appended events, not a current-state table.
_ASSIGNMENTS = [
    {
        "id": 1,
        "appointmentId": 100,
        "technicianId": 900,
        "status": "Active",
        "assignedOn": f"{_APPOINTMENT_DAY}T07:00:00Z",
    },
    {
        "id": 2,
        "appointmentId": 100,
        "technicianId": 901,
        "status": "Active",
        "assignedOn": f"{_APPOINTMENT_DAY}T07:05:00Z",
    },
    # 902 was assigned and then removed: their latest event is the removal, so
    # they must NOT appear. Filtering removal rows alone would resurrect them.
    {
        "id": 3,
        "appointmentId": 100,
        "technicianId": 902,
        "status": "Active",
        "assignedOn": f"{_APPOINTMENT_DAY}T07:10:00Z",
    },
    {
        "id": 4,
        "appointmentId": 100,
        "technicianId": 902,
        "status": "Unassigned",
        "assignedOn": f"{_APPOINTMENT_DAY}T08:00:00Z",
    },
]

#: A customer whose phone and email live in the ARRAY form ServiceTitan documents,
#: not the flat scalars. Reading only the scalars is the `job_number` mistake in a
#: different column, so the fixture pins the array form being read.
_CUSTOMER_12 = {
    "id": 12,
    "name": "Fixture Customer Ltd",
    "phoneSettings": [{"phoneNumber": "555-0199"}],
    "emailSettings": [{"email": "fixture@example.invalid"}],
}

#: A job with no location at all — `service_address`, `latitude` and `longitude`
#: are blank, and blank must stay distinguishable from "0".
_JOB_3_NO_LOCATION = {
    "id": 3,
    "jobNumber": "J-3",
    "customerId": 12,
    "jobStatus": "Completed",
    "summary": "No location on this job",
    "modifiedOn": f"{_SECOND_DAY}T00:00:00Z",
}

_APPOINTMENT_300_UNASSIGNED = {
    "id": 300,
    "jobId": 3,
    "start": f"{_SECOND_DAY}T09:00:00-05:00",
    "end": f"{_SECOND_DAY}T11:00:00-05:00",
}

#: A job whose type and business unit come from the reference tables by ID rather
#: than sitting on the job, and whose location carries coordinates. Pins the
#: `latitude`/`longitude`/`job_type`/`business_unit` columns as non-blank — a
#: column blank on every fixture row is a column whose spelling nothing checks.
_JOB_4 = {
    "id": 4,
    "jobNumber": "J-4",
    "customerId": 10,
    "locationId": 22,
    "jobTypeId": 5,
    "businessUnitId": 3,
    "jobStatus": "Scheduled",
    "summary": "Install, whole crew",
    "modifiedOn": f"{_SECOND_DAY}T00:00:00Z",
}
_LOCATION_22 = {
    "id": 22,
    "address": {
        "street": "3 Fixture Way",
        "city": "Springfield",
        "state": "IL",
        "zip": "62701",
        "latitude": 39.7817,
        "longitude": -89.6501,
    },
}
_APPOINTMENT_400 = {
    "id": 400,
    "jobId": 4,
    "start": f"{_SECOND_DAY}T13:00:00-05:00",
    "end": f"{_SECOND_DAY}T15:00:00-05:00",
}
_ASSIGNMENT_400 = {
    "id": 5,
    "appointmentId": 400,
    "technicianId": 900,
    "status": "Active",
    "assignedOn": f"{_SECOND_DAY}T12:00:00Z",
}


def job_rows() -> list[dict[str, Any]]:
    """The denormalised `jobs` rows the fixture pins, in the exporter's own order."""
    raw_jobs = RawCache()
    raw_jobs.merge([tenant_run1.JOB_1, _JOB_3_NO_LOCATION, _JOB_4])
    raw_appointments = RawCache()
    raw_appointments.merge(
        [
            tenant_run1.appointment_1(_APPOINTMENT_DAY),
            _APPOINTMENT_300_UNASSIGNED,
            _APPOINTMENT_400,
        ]
    )
    raw_assignments = RawCache()
    raw_assignments.merge([*_ASSIGNMENTS, _ASSIGNMENT_400])
    raw_customers = RawCache()
    raw_customers.merge([tenant_run1.CUSTOMER_10, _CUSTOMER_12])
    raw_locations = RawCache()
    raw_locations.merge([tenant_run1.LOCATION_20, _LOCATION_22])
    return build_job_rows(
        raw_jobs,
        raw_appointments,
        raw_assignments,
        raw_customers,
        raw_locations,
        job_types={"5": {"name": "Install"}},
        business_units={"3": {"name": "Doors"}},
    ).rows


# --- technicians --------------------------------------------------------------

_TECHNICIAN_901 = {"id": 901, "name": "Tech Two", "email": "tech2@example.invalid", "active": True}
#: No `active` field at all. Blank, never "false" — guessing false retires a live
#: technician.
_TECHNICIAN_902_UNKNOWN_ACTIVE = {"id": 902, "name": "Tech Three", "email": None}

# --- pricebook ----------------------------------------------------------------

#: An item with no `active` field at all, to pin the blank-vs-false rule on the
#: item tabs too. The tenant fixture's items are all explicitly true or false.
_SERVICE_3_UNKNOWN_ACTIVE = {
    "id": 3,
    "code": "SVC-3",
    "displayName": "Diagnostic Visit",
    "description": "Active flag absent upstream",
    "price": 89,
    "categories": [{"id": 10, "name": "Service"}],
    "assets": [],
    "modifiedOn": "2026-09-05T00:00:00Z",
}


def _report_rows() -> list[dict[str, Any]]:
    """The Job Costing Summary report's rows, keyed by its own field names.

    The live path unzips ServiceTitan's columnar ``{fields, data}`` response into
    dicts (``feeds.reporting.fetch_report_rows``); this repeats that one zip over
    the tenant fixture's own field list rather than importing an HTTP-bound
    helper, so the fixture stays client-free like every builder it feeds.
    """
    names = [field["name"] for field in tenant_financial.REPORT_FIELDS]
    return [dict(zip(names, row, strict=True)) for row in tenant_financial.REPORT_DATA]


#: Tab name -> the source records its committed fixture is generated from.
#: Every tab in ``st_exporter.contracts.tabs()`` must appear here; the test that
#: reads this asserts it, so a new tab cannot ship without a fixture.
SOURCE_RECORDS: dict[str, list[dict[str, Any]]] = {
    "jobs": [],  # filled below — needs the denormalise pass, not a raw record list
    "technicians": [
        tenant_run1.TECHNICIAN_1,
        _TECHNICIAN_901,
        # The same technician returned twice, as a page boundary can do. Deduped
        # to one row — the fixture pins that it is collapsed, not duplicated.
        dict(tenant_run1.TECHNICIAN_1),
        _TECHNICIAN_902_UNKNOWN_ACTIVE,
    ],
    "pricebook.services": [
        tenant_pricebook.SERVICE_1,
        tenant_pricebook.SERVICE_2_NO_PRICE,
        _SERVICE_3_UNKNOWN_ACTIVE,
        # No id: dropped entirely rather than written as a keyless row.
        {"code": "SVC-NO-ID", "displayName": "Dropped", "price": 1},
    ],
    "pricebook.equipment": [tenant_pricebook.EQUIPMENT_1],
    "pricebook.materials": [tenant_pricebook.MATERIAL_1],
    "pricebook.categories": [tenant_pricebook.CATEGORY_10, tenant_pricebook.CATEGORY_11],
    "accounting.invoices": [tenant_financial.INVOICE_1, tenant_financial.INVOICE_2_NO_ITEMS],
    "payroll.timesheets": (
        tenant_financial.TIMESHEETS_BY_JOB["7"] + tenant_financial.TIMESHEETS_BY_JOB["8"]
    ),
    "settings.businessUnits": tenant_financial.BUSINESS_UNITS,
    "reporting.jobCosts": _report_rows(),
}

SOURCE_RECORDS["jobs"] = job_rows()

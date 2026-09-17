"""Fixture financial tenant: invoices with line items, job timesheets, BUs, a report.

Deliberately exercises the awkward cells rather than a happy path — an invoice
line whose cost is a real zero next to one whose cost is absent, a cancelled
timesheet segment, a retired business unit, a report row with no JobNumber, and a
custom report sitting next to the built-in one with the very same name. Ticket
04's recorded fixtures can be taken straight off the grids this produces.
"""

from __future__ import annotations

import httpx
import respx

TENANT_ID = 12345

BUILTIN_CATEGORY_ID = "operations"
BUILTIN_REPORT_ID = "42"
CUSTOM_CATEGORY_ID = "accounting"
CUSTOM_REPORT_ID = "99"

INVOICE_1 = {
    "id": 500,
    "job": {"id": 7, "number": "J-7"},
    "referenceNumber": "J-7",
    "invoiceDate": "2026-09-01",
    "businessUnit": {"id": 3, "name": "Doors"},
    "items": [
        # A real zero cost next to an absent one — the pair the contract turns on.
        {
            "type": "Material",
            "sku": {"id": 55, "name": "Hinge"},
            "quantity": 4,
            "cost": 10,
            "totalCost": 40,
            "total": 120,
        },
        {
            "type": "Service",
            "sku": {"id": 56, "name": "Labour"},
            "quantity": 1,
            "cost": 0,
            "totalCost": 0,
            "total": 300,
        },
        {
            "type": "PriceModifier",
            "skuId": 57,
            "skuName": "Discount",
            "quantity": 1,
            "cost": None,
            "totalCost": None,
            "total": -50,
        },
    ],
}
INVOICE_2_NO_ITEMS = {
    "id": 501,
    "jobId": 8,
    "referenceNumber": "J-8",
    "invoiceDate": "2026-09-02",
    "items": [],
}

COMPLETED_JOBS = [{"id": 7}, {"id": 8}]

TIMESHEETS_BY_JOB = {
    "7": [
        {
            "id": 11,
            "jobId": 7,
            "appointmentId": 21,
            "technicianId": 9,
            "dispatchedOn": "2026-09-01T08:00:00Z",
            "arrivedOn": "2026-09-01T09:00:00Z",
            "doneOn": "2026-09-01T11:30:00Z",
            "canceledOn": None,
        },
        {
            "id": 12,
            "jobId": 7,
            "appointmentId": 21,
            "technicianId": 10,
            "dispatchedOn": "2026-09-01T08:00:00Z",
            "arrivedOn": None,
            "doneOn": None,
            "canceledOn": "2026-09-01T08:15:00Z",
        },
    ],
    # Answers with a bare array rather than an envelope — both forms are real.
    "8": [
        {
            "id": 13,
            "jobId": 8,
            "appointmentId": 22,
            "technicianId": 9,
            "dispatchedOn": "2026-09-02T08:00:00Z",
            "arrivedOn": "2026-09-02T08:30:00Z",
            "doneOn": "2026-09-02T10:00:00Z",
            "canceledOn": None,
        },
    ],
}

ESTIMATE_1 = {
    "id": 700,
    "job": {"id": 7, "jobNumber": "J-7"},
    "name": "Door replacement",
    "status": {"value": 2, "name": "Sold"},
    "active": True,
    "soldOn": "2026-09-01T00:00:00Z",
    "soldBy": {"id": 9},
    "subtotal": 1000,
    "total": 900,
    "modifiedOn": "2026-09-01T00:00:00Z",
    "items": [
        {
            "id": 7001,
            "sku": {"id": 55, "type": "Service", "soldHours": 2.5},
            "qty": 1,
            "total": 300,
            "unitCost": 40,
            "totalCost": 40,
        }
    ],
}
ESTIMATE_2_UNSOLD_NO_ITEMS = {
    "id": 701,
    "jobId": 8,
    "name": "Follow-up estimate",
    "status": "Open",
    "active": True,
    "items": [],
}
ESTIMATES = [ESTIMATE_1, ESTIMATE_2_UNSOLD_NO_ITEMS]

BUSINESS_UNITS = [
    {
        "id": 3,
        "name": "Doors",
        "code": "DR",
        "active": True,
        "email": "doors@example.com",
        "phoneNumber": "555-0100",
        "modifiedOn": "2026-08-01T00:00:00Z",
        "address": {"street": "1 Main St", "city": "Denver", "state": "CO", "zip": "80202"},
    },
    {"id": 4, "name": "Retired Unit", "active": False},
]

REPORT_FIELDS = [
    {"name": "JobNumber"},
    {"name": "TotalRevenue"},
    {"name": "TotalCosts"},
    {"name": "MaterialEquipmentPurchaseOrderCosts"},
    {"name": "MaterialTotals"},
    {"name": "EquipmentCosts"},
    {"name": "TechnicianName"},
]
REPORT_DATA = [
    ["J-7", 900, 500, 0, 100, 50, "Ada"],
    ["J-8", 400, 250, 250, None, None, "Grace"],
    # No JobNumber: the consumer discards it, so the exporter does too.
    ["", 0, 0, 0, 0, 0, "Nobody"],
]


def _envelope(data, has_more=False):
    return {"data": data, "hasMore": has_more, "totalCount": len(data)}


#: The columns a contractor's own "Job Costing Summary" happens to carry. Nothing
#: wrong with them — they are simply not the ones ``JOB_COST_COLUMNS`` names, so
#: every money cell would come out blank if this report were used.
NAMESAKE_FIELDS = [
    {"name": "Job"},
    {"name": "Revenue"},
    {"name": "Cost"},
]
NAMESAKE_DATA = [["J-7", 900, 500]]


def register(
    api_base: str,
    *,
    report_present: bool = True,
    custom_marked: bool = True,
) -> None:
    """Register every route the `financial` feed calls.

    ``report_present=False`` removes the built-in Job Costing Summary report and
    leaves only a contractor's custom namesake — the case the exporter must
    REFUSE rather than fall back to.

    ``custom_marked=False`` strips the ``isCustom`` marker off that namesake.
    ServiceTitan's real marker spelling is unverified (KNOWN_UNVERIFIED.md), so
    an unmarked contractor report is the case the NAME guard cannot see at all:
    it comes back as a single unambiguous match and only its COLUMNS give it
    away.
    """
    base = api_base.rstrip("/")

    respx.get(f"{base}/accounting/v2/tenant/{TENANT_ID}/invoices").mock(
        return_value=httpx.Response(200, json=_envelope([INVOICE_1, INVOICE_2_NO_ITEMS]))
    )
    respx.get(f"{base}/jpm/v2/tenant/{TENANT_ID}/jobs").mock(
        return_value=httpx.Response(200, json=_envelope(COMPLETED_JOBS))
    )
    respx.get(f"{base}/payroll/v2/tenant/{TENANT_ID}/jobs/7/timesheets").mock(
        return_value=httpx.Response(200, json={"data": TIMESHEETS_BY_JOB["7"]})
    )
    respx.get(f"{base}/payroll/v2/tenant/{TENANT_ID}/jobs/8/timesheets").mock(
        return_value=httpx.Response(200, json=TIMESHEETS_BY_JOB["8"])
    )
    respx.get(f"{base}/settings/v2/tenant/{TENANT_ID}/business-units").mock(
        return_value=httpx.Response(200, json=_envelope(BUSINESS_UNITS))
    )
    respx.get(f"{base}/sales/v2/tenant/{TENANT_ID}/estimates").mock(
        return_value=httpx.Response(200, json=_envelope(ESTIMATES))
    )

    reporting = f"{base}/reporting/v2/tenant/{TENANT_ID}"
    respx.get(f"{reporting}/report-categories").mock(
        return_value=httpx.Response(
            200,
            json=_envelope(
                [
                    {"id": BUILTIN_CATEGORY_ID, "name": "Operations"},
                    {"id": CUSTOM_CATEGORY_ID, "name": "Accounting"},
                ]
            ),
        )
    )
    builtin = [
        {"id": int(BUILTIN_REPORT_ID), "name": "Job Costing Summary"},
        {"id": 43, "name": "Technician Scorecard"},
    ]
    respx.get(f"{reporting}/report-category/{BUILTIN_CATEGORY_ID}/reports").mock(
        return_value=httpx.Response(200, json=_envelope(builtin if report_present else []))
    )
    # A contractor's own report carrying the exact same name. Must never be used.
    namesake: dict = {"id": int(CUSTOM_REPORT_ID), "name": "Job Costing Summary"}
    if custom_marked:
        namesake["isCustom"] = True
    respx.get(f"{reporting}/report-category/{CUSTOM_CATEGORY_ID}/reports").mock(
        return_value=httpx.Response(200, json=_envelope([namesake]))
    )
    # ...and it answers about itself, with its own columns, exactly like a real
    # report would. Nothing about the transport says it is the wrong report.
    respx.get(f"{reporting}/report-category/{CUSTOM_CATEGORY_ID}/reports/{CUSTOM_REPORT_ID}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": int(CUSTOM_REPORT_ID),
                "name": "Job Costing Summary",
                "fields": NAMESAKE_FIELDS,
                "parameters": [],
            },
        )
    )
    respx.post(
        f"{reporting}/report-category/{CUSTOM_CATEGORY_ID}/reports/{CUSTOM_REPORT_ID}/data"
    ).mock(
        return_value=httpx.Response(
            200, json={"fields": NAMESAKE_FIELDS, "data": NAMESAKE_DATA, "hasMore": False}
        )
    )
    respx.get(
        f"{reporting}/report-category/{BUILTIN_CATEGORY_ID}/reports/{BUILTIN_REPORT_ID}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": int(BUILTIN_REPORT_ID),
                "name": "Job Costing Summary",
                "fields": REPORT_FIELDS,
                "parameters": [
                    {"name": "From", "isRequired": True},
                    {"name": "To", "isRequired": True},
                    {"name": "DateType", "isRequired": True},
                ],
            },
        )
    )
    respx.post(
        f"{reporting}/report-category/{BUILTIN_CATEGORY_ID}/reports/{BUILTIN_REPORT_ID}/data"
    ).mock(
        return_value=httpx.Response(
            200, json={"fields": REPORT_FIELDS, "data": REPORT_DATA, "hasMore": False}
        )
    )

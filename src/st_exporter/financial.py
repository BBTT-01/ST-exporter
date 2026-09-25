"""Pure record -> row mapping for the four `financial` tabs.

Like ``pricebook.py``, this module is deliberately free of HTTP and Sheets: the
exact grid a run would write is derivable from a list of ServiceTitan records
alone. That is what makes the output recordable as a fixture (ticket 04) and what
every test in ``tests/st_exporter/test_financial.py`` exercises.

**The column names here are not this exporter's invention.** Profit Wizard's
reader already exists — ``lib/hosted/tabs.ts`` in its `feat-servicetitan-hosted`
branch — and it indexes these tabs by header NAME, using ServiceTitan's own
PascalCase field spellings rather than the snake_case of the `jobs`/`technicians`
tabs. Emitting anything else produces a tab that parses to zero rows with only a
"missing expected column" warning, which is exactly the silent-wrong-money
failure this feed is supposed to avoid. Every column below was read off that
parser:

===========================  ====================================================
tab                          columns Profit Wizard reads
===========================  ====================================================
``accounting.invoices``      ``JobId``, ``ReferenceNumber``, ``ItemType``,
                             ``ItemTotal``, ``ItemCost``, ``ItemTotalCost``,
                             ``ItemQuantity`` — **one row per invoice LINE ITEM**,
                             not per invoice
``payroll.timesheets``       ``JobId``, ``TechnicianId``, ``ArrivedOn``,
                             ``DoneOn``, ``CanceledOn``
``settings.businessUnits``   ``BusinessUnitId``, ``Name``, ``Address``, ``Active``
``reporting.jobCosts``       the Job Costing Summary report's OWN field names,
                             passed through verbatim
===========================  ====================================================

Columns beyond those are additive: Profit Wizard looks columns up by name and
ignores the rest, so the few extras carried here (invoice id, sku, dispatch time)
cost nothing and save a second ticket when someone needs them. The last three
``accounting.invoices`` columns — ``InvoiceSubTotal``, ``InvoiceSalesTax``,
``InvoiceTotal`` — are INVOICE-level, read by TrueQuote's hosted calibration, and
repeat on every line of the same invoice: summing them across lines multiplies
the invoice by its line count, so a reader dedupes by ``InvoiceId`` first.

The rules that are easiest to break, and are therefore enforced here in one place:

- Every cell is text, and **money is blank when null, never ``0``**. A zero-dollar
  line and a line with no cost are different facts; collapsing them turns missing
  cost into free work. Same rule ``pricebook._price`` follows, and Profit Wizard's
  ``toNum`` agrees — it maps ``""`` to ``null`` and ``"0"`` to ``0``.
- Ids are carried as text, never coerced to int.
- A row whose key column is blank is dropped here rather than written and dropped
  by the reader, so ``row_count`` in `_meta` means what a reconciler thinks it means.
"""

from __future__ import annotations

from typing import Any

from st_exporter.format import build_service_address, to_cell_text

CONTRACT_VERSION = "financial.v1"

#: `accounting.invoices` — one row per invoice LINE ITEM. The first seven names
#: are the ones Profit Wizard's `parseInvoicesTab` reads; the rest are additive.
INVOICE_COLUMNS: tuple[str, ...] = (
    "JobId",
    "ReferenceNumber",
    "ItemType",
    "ItemTotal",
    "ItemCost",
    "ItemTotalCost",
    "ItemQuantity",
    "InvoiceId",
    "InvoiceDate",
    "SkuId",
    "SkuName",
    "BusinessUnitId",
    # APPENDED last for TrueQuote hosted calibration: invoice-level, tax-inclusive
    # totals, repeated on every line of the same invoice (dedupe by InvoiceId).
    "InvoiceSubTotal",
    "InvoiceSalesTax",
    "InvoiceTotal",
)

#: `payroll.timesheets` — one row per timesheet segment, as returned by
#: ``payroll/v2/tenant/{id}/jobs/{jobId}/timesheets``.
TIMESHEET_COLUMNS: tuple[str, ...] = (
    "JobId",
    "TechnicianId",
    "ArrivedOn",
    "DoneOn",
    "CanceledOn",
    "Id",
    "AppointmentId",
    "DispatchedOn",
)

#: `settings.businessUnits` — the reference tab the other three join to.
BUSINESS_UNIT_COLUMNS: tuple[str, ...] = (
    "BusinessUnitId",
    "Name",
    "Address",
    "Active",
    "Email",
    "Phone",
    "Code",
    "ModifiedOn",
)

#: `reporting.jobCosts` — the Job Costing Summary report's own field names, in a
#: frozen order. ServiceTitan owns these spellings (it is a built-in report), so
#: they are as stable as any endpoint's; freezing the ORDER as well is what makes
#: two runs of the same window byte-identical. ``JobNumber`` is the key: Profit
#: Wizard's ``mapCostingRow`` returns null for a row without one.
#:
#: Any other column the report happens to carry is dropped rather than appended —
#: an unfrozen tail would change width whenever ServiceTitan added a field, and a
#: consumer that trusts column positions would silently misread every number.
JOB_COST_COLUMNS: tuple[str, ...] = (
    "JobNumber",
    "MaterialEquipmentPurchaseOrderCosts",
    "MaterialTotals",
    "EquipmentCosts",
    "TotalCosts",
    "TotalRevenue",
)

JOB_COST_KEY_COLUMN = "JobNumber"

_MONEY_COLUMNS: frozenset[str] = frozenset(
    {
        "ItemTotal",
        "ItemCost",
        "ItemTotalCost",
        "ItemQuantity",
        "InvoiceSubTotal",
        "InvoiceSalesTax",
        "InvoiceTotal",
        "MaterialEquipmentPurchaseOrderCosts",
        "MaterialTotals",
        "EquipmentCosts",
        "TotalCosts",
        "TotalRevenue",
    }
)


def build_invoice_grid(records: list[dict[str, Any]]) -> list[list[str]]:
    """Header row + one row per invoice LINE ITEM, for `accounting.invoices`.

    One invoice fans out into as many rows as it has items, because that is the
    grain Profit Wizard costs at: it sorts the lines by ``ItemType`` into material,
    equipment and service. An invoice with no items contributes no rows — there is
    nothing to cost — rather than one row of blanks that would read as a zero.
    """
    rows = [_format(row, INVOICE_COLUMNS) for row in build_invoice_rows(records)]
    return [list(INVOICE_COLUMNS)] + rows


def build_timesheet_grid(records: list[dict[str, Any]]) -> list[list[str]]:
    """Header row + one row per timesheet segment, for `payroll.timesheets`."""
    rows = [
        _format(build_timesheet_row(r), TIMESHEET_COLUMNS) for r in records if _text(r.get("jobId"))
    ]
    return [list(TIMESHEET_COLUMNS)] + rows


def build_business_unit_grid(records: list[dict[str, Any]]) -> list[list[str]]:
    """Header row + one row per business unit, for `settings.businessUnits`."""
    rows = [
        _format(build_business_unit_row(r), BUSINESS_UNIT_COLUMNS)
        for r in records
        if _text(r.get("id"))
    ]
    return [list(BUSINESS_UNIT_COLUMNS)] + rows


def build_job_cost_grid(report_rows: list[dict[str, Any]]) -> list[list[str]]:
    """Header row + one row per report row, for `reporting.jobCosts`.

    ``report_rows`` are the report's own rows already keyed by its own field names
    (``feeds/reporting.fetch_report_rows`` does the columnar unzip). This narrows
    them to the frozen column set and drops any row with no ``JobNumber``, which
    the consumer would discard anyway.
    """
    rows = []
    for report_row in report_rows:
        if not _text(report_row.get(JOB_COST_KEY_COLUMN)):
            continue
        rows.append([_cell(column, report_row.get(column)) for column in JOB_COST_COLUMNS])
    return [list(JOB_COST_COLUMNS)] + rows


def build_invoice_rows(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Flatten invoices into one dict per line item."""
    rows: list[dict[str, str]] = []
    for record in records:
        job_id = _ref_id(record, "job", "jobId")
        reference_number = _text(record.get("referenceNumber"))
        if not job_id and not reference_number:
            # Profit Wizard keys a line on one or the other; with neither it can't
            # be attributed to a job, so it is not cost data.
            continue
        for item in record.get("items") or []:
            if not isinstance(item, dict):
                continue
            rows.append(_invoice_item_row(record, item, job_id, reference_number))
    return rows


def _invoice_item_row(
    record: dict[str, Any],
    item: dict[str, Any],
    job_id: str,
    reference_number: str,
) -> dict[str, str]:
    """Map one invoice + one of its line items onto the invoice-tab columns.

    ``ItemTotalCost`` is the EXTENDED cost (cost x quantity) and ``ItemCost`` the
    per-unit one. Profit Wizard prefers the extended figure and only multiplies
    when it is missing, so the two must never be swapped — doing so understates
    cost by the quantity factor on every multi-unit line.
    """
    return {
        "JobId": job_id,
        "ReferenceNumber": reference_number,
        "ItemType": _text(item.get("type")),
        "ItemTotal": _money(item.get("total")),
        "ItemCost": _money(item.get("cost")),
        "ItemTotalCost": _money(item.get("totalCost")),
        "ItemQuantity": _money(item.get("quantity")),
        "InvoiceId": _text(record.get("id")),
        "InvoiceDate": _text(record.get("invoiceDate")),
        "SkuId": _sku(item, "id", "skuId"),
        "SkuName": _sku(item, "name", "skuName"),
        "BusinessUnitId": _ref_id(record, "businessUnit", "businessUnitId"),
        "InvoiceSubTotal": _money(record.get("subTotal")),
        "InvoiceSalesTax": _money(record.get("salesTax")),
        "InvoiceTotal": _money(record.get("total")),
    }


def build_timesheet_row(record: dict[str, Any]) -> dict[str, str]:
    """Map one job timesheet segment onto the timesheet-tab columns.

    No derived ``Hours`` column: Profit Wizard computes hours from
    ``ArrivedOn``/``DoneOn`` itself (skipping any segment with a ``CanceledOn``),
    and duplicating that arithmetic here would bake this exporter's rounding into
    a labour number Profit Wizard owns — the exact reconciliation problem this
    ticket exists to avoid.
    """
    return {
        "JobId": _text(record.get("jobId")),
        "TechnicianId": _text(record.get("technicianId")),
        "ArrivedOn": _text(record.get("arrivedOn")),
        "DoneOn": _text(record.get("doneOn")),
        "CanceledOn": _text(record.get("canceledOn")),
        "Id": _text(record.get("id")),
        "AppointmentId": _text(record.get("appointmentId")),
        "DispatchedOn": _text(record.get("dispatchedOn")),
    }


def build_business_unit_row(record: dict[str, Any]) -> dict[str, str]:
    """Map one ServiceTitan business unit onto the reference-tab columns.

    ``Address`` is the same single-line join the `jobs` tab's ``service_address``
    uses, so a business unit's address and a job's address are formatted the same
    way in the same Sheet.

    ``Code`` is **permanently blank, and that is not a bug in this mapping**:
    ``TenantSettings.V2.BusinessUnitResponse`` has no ``code`` field, nor does its
    export twin. Do not repoint it at ``accountCode`` or ``conceptCode`` — those
    belong to the TENANT and are the same string on every unit, so the column
    would look populated and mean nothing. The reason, and the plan to drop the
    column at `financial.v2`, are in ``blank_columns.ALL_BLANK_OK`` and
    KNOWN_UNVERIFIED.md. The read stays so the column fills itself in if
    ServiceTitan ever adds the field.
    """
    return {
        "BusinessUnitId": _text(record.get("id")),
        "Name": _text(record.get("name")),
        "Address": build_service_address(_obj(record.get("address")) or None),
        "Active": _bool_text(record.get("active")),
        "Email": _text(record.get("email")),
        "Phone": _text(record.get("phoneNumber") or record.get("phone")),
        "Code": _text(record.get("code")),
        "ModifiedOn": _text(record.get("modifiedOn")),
    }


def _format(row: dict[str, str], columns: tuple[str, ...]) -> list[str]:
    return [row.get(column, "") for column in columns]


def _cell(column: str, value: Any) -> str:
    return _money(value) if column in _MONEY_COLUMNS else _text(value)


def _text(value: Any) -> str:
    return to_cell_text(value)


def _money(value: Any) -> str:
    """A numeric amount as text — **blank when null, never ``0``**.

    A genuine zero still renders as ``"0"``; only an absent/null amount is blank.
    Profit Wizard's ``toNum`` reads ``""`` as null and ``"0"`` as zero, and its
    cost maths branches on exactly that difference (a ``MaterialEquipmentPurchase
    OrderCosts`` of 0 falls through to the ``MaterialTotals + EquipmentCosts``
    path, a null one does not), so the distinction is load-bearing, not tidiness.
    """
    if value is None:
        return ""
    return to_cell_text(value)


def _bool_text(value: Any) -> str:
    """Lowercase ``true``/``false``; blank when the field is absent.

    Blank rather than defaulting to ``false``, because ``false`` means "retired"
    to the consumer and guessing it would hide a live business unit's revenue.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return to_cell_text(value)
    text = str(value).strip().lower()
    if text in {"true", "false"}:
        return text
    return ""


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ref_id(record: dict[str, Any], nested_key: str, flat_key: str) -> str:
    """The id of a related record, from either the nested object or the flat field.

    ServiceTitan returns ``job``/``businessUnit`` as nested objects on an invoice,
    but the flat ``jobId``/``businessUnitId`` form has been observed too. Accepting
    both costs one ``or``; assuming one silently empties a join column.
    """
    nested = _obj(record.get(nested_key)).get("id")
    if nested is not None:
        return to_cell_text(nested)
    return to_cell_text(record.get(flat_key))


def _sku(item: dict[str, Any], nested_field: str, flat_key: str) -> str:
    """A line item's sku id/name, from the nested ``sku`` object or the flat field."""
    nested = _obj(item.get("sku")).get(nested_field)
    if nested is not None:
        return to_cell_text(nested)
    return to_cell_text(item.get(flat_key))

"""Pure record -> row mapping for the `sales.estimates` tab.

Like ``financial.py`` and ``pricebook.py``, this module is deliberately free of
HTTP and Sheets: the exact grid a run would write is derivable from a list of
ServiceTitan estimate records alone, which is what makes it recordable as a
contract fixture and independently testable.

**One row per estimate ITEM**, not per estimate — the same fan-out
``financial.build_invoice_rows`` already does for invoice line items, and for the
same reason: Profit Wizard's ``normalizeEstimate``/``aggregateQuotes`` cost at the
item grain (``items[].sku.type``, ``items[].sku.soldHours``, ``items[].totalCost``
/ ``unitCost`` x ``quantity``). An estimate with no items still gets exactly one
row, with every ``Item*`` column blank, so the estimate itself (its id, status,
whether it sold) is never lost for having nothing to cost yet.

**Every field name here beyond the estimate's own id is an unverified guess**
(see ``KNOWN_UNVERIFIED.md``, "sales.estimates field spellings") — there was no
real ServiceTitan tenant to record a response from. Each is read from the
spelling ServiceTitan's other endpoints use for the same shape of fact
(``sku.id``/``sku.type`` mirrors an invoice line's ``sku``; ``qty`` mirrors the
Estimates API's documented request shape) with a flatter alternate tried second,
the same widen-don't-guess-once shape ``denormalize._contact_detail`` uses. A
wrong guess here costs a blank column, not a wrong number: everything money-typed
runs through ``_money`` (blank when null, never ``0``), and the blank-column
detector (``blank_columns.py``) reports it loudly on the first real tenant.

Cell rules are identical to every other tab: every cell is text, money and hours
are blank when absent (never ``0``), booleans are lowercase, and a row missing its
key column (``id``) is dropped rather than written blank.
"""

from __future__ import annotations

from typing import Any

from st_exporter.format import to_cell_text

CONTRACT_VERSION = "sales.v1"

#: `sales.estimates` — one row per estimate ITEM; an estimate with no items still
#: writes one row with the `Item*` columns blank. Column order is frozen by
#: ``export-columns-spec.md`` — Profit Wizard's hosted reader depends on it.
ESTIMATE_COLUMNS: tuple[str, ...] = (
    "EstimateId",
    "JobId",
    "JobNumber",
    "EstimateName",
    "Status",
    "Active",
    "SoldOn",
    "SoldById",
    "Subtotal",
    "Total",
    "ItemId",
    "ItemSkuId",
    "ItemSkuType",
    "ItemSkuSoldHours",
    "ItemQuantity",
    "ItemTotal",
    "ItemUnitCost",
    "ItemTotalCost",
    "ModifiedOn",
)

ESTIMATE_KEY_COLUMNS: tuple[str, ...] = ("EstimateId", "ItemId")

_EMPTY_ITEM: dict[str, str] = {
    "ItemId": "",
    "ItemSkuId": "",
    "ItemSkuType": "",
    "ItemSkuSoldHours": "",
    "ItemQuantity": "",
    "ItemTotal": "",
    "ItemUnitCost": "",
    "ItemTotalCost": "",
}


def build_estimate_grid(records: list[dict[str, Any]]) -> list[list[str]]:
    """Header row + one row per estimate ITEM, for `sales.estimates`."""
    rows = [_format(row) for row in build_estimate_rows(records)]
    return [list(ESTIMATE_COLUMNS)] + rows


def build_estimate_rows(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Flatten estimates into one dict per item (or one blank-item dict apiece)."""
    rows: list[dict[str, str]] = []
    for record in records:
        estimate_id = _text(record.get("id"))
        if not estimate_id:
            # No id: cannot be joined to anything downstream, same rule
            # `financial.build_invoice_rows` applies to an unattributable invoice.
            continue
        common = _estimate_row(record, estimate_id)
        items = [item for item in (record.get("items") or []) if isinstance(item, dict)]
        if not items:
            rows.append({**common, **_EMPTY_ITEM})
            continue
        for item in items:
            rows.append({**common, **_item_row(item)})
    return rows


def _estimate_row(record: dict[str, Any], estimate_id: str) -> dict[str, str]:
    return {
        "EstimateId": estimate_id,
        "JobId": _ref_id(record, "job", "jobId"),
        "JobNumber": _job_number(record),
        "EstimateName": _text(record.get("name")),
        "Status": _status(record.get("status")),
        "Active": _bool_text(record.get("active")),
        "SoldOn": _text(record.get("soldOn")),
        "SoldById": _ref_id(record, "soldBy", "soldById"),
        "Subtotal": _money(record.get("subtotal")),
        "Total": _estimate_total(record),
        "ModifiedOn": _text(record.get("modifiedOn")),
    }


def _item_row(item: dict[str, Any]) -> dict[str, str]:
    """One estimate item onto the `Item*` columns.

    ``ItemSkuSoldHours`` is a numeric TEXT cell, not money — but the same "blank
    when null, never 0" rule applies (see the module docstring): an item whose
    hours are unknown must not be read as a zero-hour item.
    """
    sku = _obj(item.get("sku"))
    return {
        "ItemId": _text(item.get("id")),
        "ItemSkuId": _first(sku.get("id"), item.get("skuId")),
        "ItemSkuType": _first(sku.get("type"), item.get("skuType")),
        "ItemSkuSoldHours": _money(_first_value(sku, "soldHours", item, "skuSoldHours")),
        "ItemQuantity": _money(_first_value(item, "qty", item, "quantity")),
        "ItemTotal": _money(item.get("total")),
        "ItemUnitCost": _money(item.get("unitCost")),
        "ItemTotalCost": _money(item.get("totalCost")),
    }


def _format(row: dict[str, str]) -> list[str]:
    return [row.get(column, "") for column in ESTIMATE_COLUMNS]


def _text(value: Any) -> str:
    return to_cell_text(value)


def _first(*values: Any) -> str:
    """The first non-``None`` value, as cell text — blank if every one is absent."""
    for value in values:
        if value is not None:
            return to_cell_text(value)
    return ""


def _first_value(
    primary_source: dict[str, Any],
    primary_key: str,
    fallback_source: dict[str, Any],
    fallback_key: str,
) -> Any:
    if primary_key in primary_source and primary_source[primary_key] is not None:
        return primary_source[primary_key]
    return fallback_source.get(fallback_key)


def _money(value: Any) -> str:
    """A numeric amount (or hours) as text — blank when null, never ``0``.

    See ``financial._money``: the same distinction, applied here to
    ``ItemSkuSoldHours``/``ItemQuantity`` as well as the dollar columns, because
    an unknown hours figure must not read as a zero-hour item.
    """
    if value is None:
        return ""
    return to_cell_text(value)


def _bool_text(value: Any) -> str:
    """Lowercase ``true``/``false``; blank when the field is absent.

    Blank rather than defaulting to ``false`` — a blank ``Active`` means unknown,
    matching every other boolean column in this exporter (see `financial._bool_text`).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return to_cell_text(value)
    text = str(value).strip().lower()
    if text in {"true", "false"}:
        return text
    return ""


def _status(value: Any) -> str:
    """``estimate.status.name`` (or ``.value``) verbatim; the bare value if it is
    already a scalar rather than an object."""
    if isinstance(value, dict):
        name = value.get("name")
        return _text(name if name is not None else value.get("value"))
    return _text(value)


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ref_id(record: dict[str, Any], nested_key: str, flat_key: str) -> str:
    """The id of a related record: nested object, bare scalar, or flat ``*Id`` field.

    Same shape as ``financial._ref_id``, widened one step: ServiceTitan nests some
    related records (``job: {id}``), flattens others (``jobId``), and on an
    estimate returns ``soldBy`` as the bare employee id — this repo's own CLI
    docs sell an estimate with ``{"soldBy": 10}`` and filter the list with
    ``soldById=10``. A scalar under the nested key is therefore the id itself,
    not a malformed object to skip.
    """
    value = record.get(nested_key)
    if isinstance(value, dict):
        nested = value.get("id")
        if nested is not None:
            return to_cell_text(nested)
    elif value is not None and not isinstance(value, (list, tuple)):
        return to_cell_text(value)
    return to_cell_text(record.get(flat_key))


def _estimate_total(record: dict[str, Any]) -> str:
    """``estimate.total`` when present; otherwise ``subtotal + tax`` when BOTH are.

    The Estimates API response carries ``subtotal`` and ``tax`` as separate
    figures and (as far as is known without a recorded response — see
    ``KNOWN_UNVERIFIED.md``) no ``total`` of its own, so reading ``total`` alone
    would leave the column blank on every row. The sum is only taken when both
    parts are real numbers: a missing tax is "unknown", not 0, and blank is not
    zero here any more than on any other money column.
    """
    total = record.get("total")
    if total is not None:
        return _money(total)
    subtotal, tax = _number(record.get("subtotal")), _number(record.get("tax"))
    if subtotal is None or tax is None:
        return ""
    return _money(subtotal + tax)


def _number(value: Any) -> int | float | None:
    """``value`` as a number, or ``None`` for anything that is not one (bools included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _job_number(record: dict[str, Any]) -> str:
    """A job's ``jobNumber`` off the nested ``job`` object, or the estimate's own
    flat ``jobNumber``, mirroring how `jobs.job_number` widens across spellings."""
    nested = _obj(record.get("job")).get("jobNumber")
    if nested is not None:
        return to_cell_text(nested)
    return to_cell_text(record.get("jobNumber"))

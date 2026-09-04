"""Cell-text formatting for the Export Store.

Every cell in the Export Store is written as text — Sheets is asked to store the
value literally (``value_input_option=RAW``, see ``sheets.py``) rather than parse
it, so a `"40.7128"` cell is never silently reinterpreted as a number and a
`"2026-09-03T10:00:00-05:00"` cell is never truncated to a bare date. ``to_cell_text``
is the single funnel every cell passes through so that rule can't be violated ad hoc
elsewhere in the codebase.
"""

from __future__ import annotations

from typing import Any

# Column order is frozen by spec.md — TradeRated's reader depends on it exactly.
JOB_COLUMNS: tuple[str, ...] = (
    "st_job_id",
    "st_appointment_id",
    "job_number",
    "st_technician_id",
    "customer_name",
    "customer_phone",
    "customer_email",
    "service_address",
    "latitude",
    "longitude",
    "appointment_start",
    "appointment_end",
    "job_status",
    "job_type",
    "summary",
    "business_unit",
    "modified_on",
)

TECHNICIAN_COLUMNS: tuple[str, ...] = ("st_technician_id", "name", "email", "active")

_ADDRESS_PARTS = ("street", "unit", "city", "state", "zip")


def to_cell_text(value: Any) -> str:
    """Format one value as the literal text that goes into a Sheet cell.

    ``None`` -> ``""`` (blank, never the string "None"). Booleans serialise as
    lowercase ``"true"``/``"false"`` to match ServiceTitan's own JSON convention,
    not Python's ``str(bool)`` (which would give ``"True"``). Everything else goes
    through ``str()`` — floats use Python's shortest round-trip ``repr`` via
    ``str()``, which is deterministic across runs given the same input.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_service_address(address: dict[str, Any] | None) -> str:
    """Join a ServiceTitan address object into the single-line ``service_address`` cell.

    Skips parts that are missing or blank rather than emitting empty segments
    (e.g. no dangling ``", "`` when ``unit`` is absent).
    """
    if not address:
        return ""
    parts = [str(address[key]).strip() for key in _ADDRESS_PARTS if address.get(key)]
    return ", ".join(parts)


def format_job_row(row: dict[str, Any]) -> list[str]:
    """Render one denormalised job dict into the exact `jobs` tab column order."""
    return [to_cell_text(row.get(col)) for col in JOB_COLUMNS]


def format_technician_row(row: dict[str, Any]) -> list[str]:
    """Render one technician dict into the exact `technicians` tab column order."""
    return [to_cell_text(row.get(col)) for col in TECHNICIAN_COLUMNS]

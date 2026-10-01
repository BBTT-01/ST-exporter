"""Pure record -> row mapping for the `dispatch.nonJobAppointments` tab.

A ServiceTitan **non-job appointment** is a block on a technician's calendar that
is not a job: lunch, training, a meeting, PTO, a shop day. Profit Wizard's
dispatch board needs them to know when a technician is NOT available, which the
`jobs` tab cannot tell it — a technician with no job at 2pm is either free or at
the dentist, and only this tab says which.

Like ``sales.py`` and ``pricebook_bom.py``, this module is deliberately free of
HTTP and Sheets: the exact grid a run would write is derivable from a list of
records alone, which is what makes it recordable as a contract fixture. The one
fact that does not come off the record — the timesheet code's NAME — is resolved
by the fetch (``feeds/dispatch.py``) and stamped onto each record under
:data:`TIMESHEET_CODE_NAME_FIELD`, exactly as ``fetch_timesheets`` stamps
``jobId``, so the builder stays a single-argument function.

**The record shape is confirmed**, read-only, against a production tenant on
2026-10-01 (see ``KNOWN_UNVERIFIED.md``): ``id, technicianId, start, name,
duration, timesheetCodeId, summary, clearDispatchBoard, clearTechnicianView,
removeTechnicianFromCapacityPlanning, allDay, showOnTechnicianSchedule, active,
createdOn, modifiedOn, createdById``. There is **no** ``end`` field.

Two cells are passed through VERBATIM rather than normalised, because each has a
shape a well-meaning conversion would damage:

- ``start`` is always UTC with a ``Z``, sometimes with milliseconds. It is
  written exactly as ServiceTitan sent it, never re-rendered.
- ``duration`` is a .NET ``TimeSpan`` string — ``01:00:00``,
  ``02:10:00.5000000`` — and all-day records carry ``23:59:59``. The end of an
  appointment is ``start + duration``, and that arithmetic is the consumer's:
  converting it here would mean picking a rounding for the seven-digit fraction
  and a meaning for an all-day block on the exporter's side of the contract.

Cell rules are every other tab's: every cell is text, ``None`` is blank,
booleans are lowercase, ids are text. ``timesheet_code_id`` is blank for ``0``,
because ServiceTitan uses ``0`` to mean "no code" — a ``"0"`` cell would read as
a code whose id is zero.
"""

from __future__ import annotations

from typing import Any

from st_exporter.format import to_cell_text

CONTRACT_VERSION = "dispatch.v1"

NON_JOB_APPOINTMENTS_TAB = "dispatch.nonJobAppointments"

#: Column order is frozen: Profit Wizard's hosted reader (`lib/hosted/tabs.ts`)
#: matches these exact header names.
NON_JOB_APPOINTMENT_COLUMNS: tuple[str, ...] = (
    "st_non_job_appointment_id",
    "st_technician_id",
    "start",
    "duration",
    "all_day",
    "active",
    "remove_technician_from_capacity_planning",
    "name",
    "timesheet_code_id",
    "timesheet_code_name",
)

NON_JOB_APPOINTMENT_KEY_COLUMNS: tuple[str, ...] = ("st_non_job_appointment_id",)

#: Not a ServiceTitan field. The resolved timesheet code name, stamped onto each
#: record by ``feeds.dispatch.fetch_non_job_appointments`` from the payroll
#: timesheet-codes list. Absent or ``None`` means unresolved, and the cell is blank.
TIMESHEET_CODE_NAME_FIELD = "timesheetCodeName"


def build_non_job_grid(records: list[dict[str, Any]]) -> list[list[str]]:
    """Header row + one row per non-job appointment, for `dispatch.nonJobAppointments`.

    Repeating events arrive from ServiceTitan as one record per occurrence, so
    one row is one occurrence. A record with no ``id`` is dropped rather than
    written with a blank key, and a repeated ``id`` — the same record returned on
    two pages while the list shifted underneath the pagination — is written
    once, first occurrence kept, so the tab's row key holds.
    """
    rows: list[list[str]] = []
    seen: set[str] = set()
    for record in records:
        appointment_id = to_cell_text(record.get("id")).strip()
        if not appointment_id or appointment_id in seen:
            continue
        seen.add(appointment_id)
        rows.append(
            [
                appointment_id,
                to_cell_text(record.get("technicianId")),
                to_cell_text(record.get("start")),
                to_cell_text(record.get("duration")),
                to_cell_text(record.get("allDay")),
                to_cell_text(record.get("active")),
                to_cell_text(record.get("removeTechnicianFromCapacityPlanning")),
                to_cell_text(record.get("name")),
                timesheet_code_id(record.get("timesheetCodeId")),
                to_cell_text(record.get(TIMESHEET_CODE_NAME_FIELD)),
            ]
        )
    return [list(NON_JOB_APPOINTMENT_COLUMNS)] + rows


def timesheet_code_id(value: Any) -> str:
    """``timesheetCodeId`` as cell text — blank for ``None`` and for ``0``.

    ``0`` is ServiceTitan's "no timesheet code", confirmed on a live tenant, so
    it is the absence of an id rather than an id. Shared with the fetch, which
    uses the same rule to decide whether there is a code name to resolve.
    """
    text = to_cell_text(value).strip()
    return "" if text in ("", "0") else text

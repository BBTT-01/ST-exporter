"""Join jobs+appointments+assignments+customers+locations into `jobs` tab rows.

One output row per appointment (row cardinality follows `_raw_appointments`), with
customer and location detail joined in — there is no customers tab and no locations
tab in the Export Store (spec.md). The window filter is applied by the caller
(``run.py``) against the ``appointment_start`` this module puts on each row; this
module itself never looks at any window boundary.

A few field mappings here are marked as assumptions because they can't be confirmed
without a real ServiceTitan tenant (see ``KNOWN_UNVERIFIED.md``): the exact
`appointment-assignments` status values that mean "removed", whether `jobTypeName`/
`businessUnitName` are present directly on job records or need a reference-table
join, the exact JSON path for a location's coordinates, and the exact `jobStatus`
values that mean "cancelled/deleted" (see `_EXCLUDED_JOB_STATUSES` below).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from st_exporter.feeds.raw_cache import RawCache
from st_exporter.format import build_service_address

_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def _parse_utc_datetime(value: str | None) -> datetime:
    """Parse an ISO 8601 timestamp to a UTC-aware datetime for sort comparisons.

    Missing or unparsable values sort as the earliest possible instant, so they
    never win a "most recent" comparison against a real timestamp. Mirrors
    ``window._parse_utc_date``'s UTC-conversion approach, reimplemented locally
    (not imported) since that one returns a ``date``, not a ``datetime``, and is
    private to its own module.
    """
    if not value:
        return _EARLIEST
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return _EARLIEST
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class DenormalizeResult:
    rows: list[dict[str, Any]]
    skipped_no_job: int


# Assignment statuses treated as "no longer assigned". Unconfirmed against real
# ServiceTitan data — see KNOWN_UNVERIFIED.md. Compared case-insensitively.
_REMOVED_ASSIGNMENT_STATUSES = {"unassigned", "removed", "cancelled", "canceled"}

# job_status values that mean the job is cancelled/deleted and must not keep being
# re-written to the jobs tab forever. Unconfirmed against real ServiceTitan data —
# see KNOWN_UNVERIFIED.md. Compared case-insensitively. Without this, a cancelled
# job with a future-dated appointment would pass in_window() indefinitely, since
# nothing else in this pipeline ever removes a record once seen.
_EXCLUDED_JOB_STATUSES = {"canceled", "cancelled"}


def _first_present(*sources: dict[str, Any] | None, keys: tuple[str, ...]) -> Any:
    """Return the first non-``None`` value for any of ``keys`` across ``sources``,
    tried in order. Distinguishes "absent" (keep looking / return ``None``) from a
    legitimate falsy value like ``0`` or ``""``, which is returned as-is."""
    for source in sources:
        if not source:
            continue
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]
    return None


def _coordinate(location: dict[str, Any] | None) -> tuple[Any, Any]:
    """Latitude/longitude for a location, or (None, None) if ServiceTitan has none.

    Tries the nested ``address.{latitude,longitude}`` path first (this API nests
    the rest of the address there — see ``LOCATION_COLUMNS`` in
    ``st_cli/commands/crm.py``), then a top-level fallback. Returning ``None``
    (not ``0``) when absent is required by the frozen contract — implemented via
    dict membership, not truthiness, so a real ``0.0`` at the equator or prime
    meridian is never mistaken for "missing".
    """
    if not location:
        return None, None
    address = location.get("address") or {}
    lat = _first_present(address, location, keys=("latitude", "lat"))
    lng = _first_present(address, location, keys=("longitude", "lng", "lon"))
    return lat, lng


def _active_technician_id(assignments: list[dict[str, Any]]) -> str | None:
    """Resolve the single current technician for an appointment from its assignment
    events (an append-only assign/unassign feed, not a current-state table).

    Discards records whose status indicates removal, then takes the remaining
    record with the latest ``assignedOn``. A genuine tie (concurrent multi-tech
    assignment) breaks on the lowest technician id — a deterministic, documented
    simplification the frozen contract's single `st_technician_id` column forces;
    flagged in KNOWN_UNVERIFIED.md for a business-rule sanity check once real
    assignment data exists.
    """
    active = [
        a
        for a in assignments
        if str(a.get("status", "")).strip().lower() not in _REMOVED_ASSIGNMENT_STATUSES
    ]
    if not active:
        return None

    def sort_key(a: dict[str, Any]) -> tuple[datetime, int]:
        assigned_on = _parse_utc_datetime(a.get("assignedOn"))
        technician_id = a.get("technicianId")
        if technician_id is None:
            tech_id_int = 0
        else:
            try:
                tech_id_int = int(technician_id)
            except (TypeError, ValueError):
                tech_id_int = 0
        return (assigned_on, -tech_id_int)

    best = max(active, key=sort_key)
    technician_id = best.get("technicianId")
    return None if technician_id is None else str(technician_id)


def _job_type_name(job: dict[str, Any], job_types: dict[str, dict[str, Any]]) -> str | None:
    name = job.get("jobTypeName")
    if name:
        return str(name)
    job_type_id = job.get("jobTypeId")
    if job_type_id is None:
        return None
    ref = job_types.get(str(job_type_id))
    return str(ref["name"]) if ref and ref.get("name") else None


def _business_unit_name(
    job: dict[str, Any], business_units: dict[str, dict[str, Any]]
) -> str | None:
    name = job.get("businessUnitName")
    if name:
        return str(name)
    bu_id = job.get("businessUnitId")
    if bu_id is None:
        return None
    ref = business_units.get(str(bu_id))
    return str(ref["name"]) if ref and ref.get("name") else None


def build_job_rows(
    raw_jobs: RawCache,
    raw_appointments: RawCache,
    raw_assignments: RawCache,
    raw_customers: RawCache,
    raw_locations: RawCache,
    job_types: dict[str, dict[str, Any]] | None = None,
    business_units: dict[str, dict[str, Any]] | None = None,
) -> DenormalizeResult:
    """Build the full candidate set of `jobs` tab rows (before the window filter).

    Rows are dicts keyed by the `jobs` tab's column names (see
    ``format.JOB_COLUMNS``), sorted by (``st_job_id``, ``st_appointment_id``) for
    deterministic output — required so an appointment untouched between two runs
    formats to the exact same row in the exact same position (``run.py``'s
    "byte-identical unchanged rows"). ``skipped_no_job`` counts appointments whose
    job hasn't arrived in the raw cache yet; the caller should log it, not treat it
    as an error — those appointments reappear automatically once their job's delta
    lands.
    """
    job_types = job_types or {}
    business_units = business_units or {}

    assignments_by_appointment: dict[str, list[dict[str, Any]]] = {}
    for assignment in raw_assignments.values():
        appointment_id = assignment.get("appointmentId")
        if appointment_id is None:
            continue
        assignments_by_appointment.setdefault(str(appointment_id), []).append(assignment)

    rows: list[dict[str, Any]] = []
    skipped_no_job = 0

    for appointment in raw_appointments.values():
        job_id = appointment.get("jobId")
        job = raw_jobs.get(job_id) if job_id is not None else None
        if job is None:
            # Job hasn't landed yet on this side (e.g. independent cursors caught
            # the appointment before the job). Idempotent design: skip for now,
            # picked up automatically once the jobs feed's delta includes it.
            skipped_no_job += 1
            continue

        if str(job.get("jobStatus", "")).strip().lower() in _EXCLUDED_JOB_STATUSES:
            # Cancelled/deleted: must not keep being re-written to the jobs tab.
            # Idempotent design, same as skipped_no_job — if ServiceTitan ever
            # un-cancels the job, the next jobStatus delta un-excludes it too.
            continue

        customer = raw_customers.get(job.get("customerId"))
        location = raw_locations.get(job.get("locationId"))
        latitude, longitude = _coordinate(location)

        appointment_id = appointment.get("id")
        technician_id = _active_technician_id(
            assignments_by_appointment.get(str(appointment_id), [])
        )

        rows.append(
            {
                "st_job_id": job.get("id"),
                "st_appointment_id": appointment_id,
                "job_number": job.get("number"),
                "st_technician_id": technician_id,
                "customer_name": customer.get("name") if customer else None,
                "customer_phone": customer.get("phone") if customer else None,
                "customer_email": customer.get("email") if customer else None,
                "service_address": build_service_address(location.get("address"))
                if location
                else "",
                "latitude": latitude,
                "longitude": longitude,
                "appointment_start": appointment.get("start"),
                "appointment_end": appointment.get("end"),
                "job_status": job.get("jobStatus"),
                "job_type": _job_type_name(job, job_types),
                "summary": job.get("summary"),
                "business_unit": _business_unit_name(job, business_units),
                "modified_on": appointment.get("modifiedOn") or job.get("modifiedOn"),
                # Not a contract column; used by run.py to sort deterministically
                # without re-deriving ints from formatted text.
                "_sort_key": _sort_key(job.get("id"), appointment_id),
            }
        )

    rows.sort(key=lambda r: r["_sort_key"])
    for row in rows:
        del row["_sort_key"]
    return DenormalizeResult(rows=rows, skipped_no_job=skipped_no_job)


def _sort_key(job_id: Any, appointment_id: Any) -> tuple[int, int]:
    def as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return (as_int(job_id), as_int(appointment_id))

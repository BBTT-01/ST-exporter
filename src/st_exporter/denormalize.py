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


def _assignment_sort_key(a: dict[str, Any]) -> tuple[datetime, int]:
    """Order assignment events: later ``assignedOn`` first, then lowest technician id."""
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


def _active_technician_ids(assignments: list[dict[str, Any]]) -> list[str]:
    """Resolve EVERY currently-assigned technician for an appointment from its
    assignment events (an append-only assign/unassign feed, not a current-state
    table).

    ServiceTitan genuinely supports multi-technician appointments — an install
    crew of three is one appointment with three live assignments — so this returns
    all of them and `build_job_rows` emits one row per technician. The
    single-technician predecessor kept only the most recently assigned, which
    silently hid the job from the rest of the crew.

    Removal is resolved PER TECHNICIAN, not by filtering the event list. The feed
    appends rather than mutates, so a technician who was assigned and later
    unassigned still has a live "Active" record sitting in it; filtering only the
    removal rows would resurrect them. Their LATEST event decides, and they count
    as assigned only if that event is not a removal.

    Order is most-recently-assigned first, ties on lowest technician id — the old
    single-value tie-break, kept so row order stays deterministic between runs.
    """
    latest_per_technician: dict[str, dict[str, Any]] = {}
    for assignment in assignments:
        technician_id = assignment.get("technicianId")
        if technician_id is None:
            continue
        key = str(technician_id)
        incumbent = latest_per_technician.get(key)
        if incumbent is None or _assignment_sort_key(assignment) >= _assignment_sort_key(incumbent):
            latest_per_technician[key] = assignment

    still_assigned = [
        (key, assignment)
        for key, assignment in latest_per_technician.items()
        if str(assignment.get("status", "")).strip().lower() not in _REMOVED_ASSIGNMENT_STATUSES
    ]
    still_assigned.sort(key=lambda pair: _assignment_sort_key(pair[1]), reverse=True)
    return [key for key, _ in still_assigned]


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
        # One row per assigned technician. An appointment with a crew of three
        # yields three rows sharing an `st_appointment_id`; `[None]` preserves the
        # single unassigned-appointment row the consumer already skips.
        technician_ids: list[str | None] = list(
            _active_technician_ids(assignments_by_appointment.get(str(appointment_id), []))
        ) or [None]

        for technician_id in technician_ids:
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

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
from typing import Any, Mapping

from st_exporter.feeds.contacts import (
    EMAIL_TYPES_IN_PREFERENCE_ORDER,
    PHONE_TYPES_IN_PREFERENCE_ORDER,
    select_contact_value,
)
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

#: Where a JPM job record carries its completion instant, in priority order.
#:
#: ``completedOn`` is the documented spelling and is the one expected to match.
#: It is well-evidenced rather than guessed: the sibling query parameter
#: ``completedOnOrAfter`` on ``GET /jpm/v2/tenant/{tenant}/jobs`` is CONFIRMED
#: against the published ``tenant-jpm-v2`` OpenAPI description (see
#: ``feeds/financial.JOB_COMPLETED_PARAM`` and ``KNOWN_UNVERIFIED.md``), a
#: parameter that filters on a field of that name, and
#: ``financial.warn_if_older_than_window`` already reads ``completedOn`` off
#: live job records from that same endpoint.
#:
#: What is NOT confirmed is that the **export** change-feed
#: (``/jpm/v2/tenant/{tenant}/export/jobs``, which is what actually fills
#: ``_raw_jobs`` and therefore this tab) spells it identically to the list
#: endpoint. The alternate is carried for the same reason ``job_number`` now
#: reads ``jobNumber`` before ``number``: a single guessed key is exactly how
#: that column stayed blank on all 2431 rows of a live Sheet for the life of the
#: feature. Both names mean the same fact, so trying both cannot pick up a
#: DIFFERENT one — the failure mode that makes widening dangerous elsewhere.
#:
#: If neither matches, the tenant's next run says so by itself: ``completed_on``
#: is not in ``blank_columns.ALL_BLANK_OK``, so a whole-column blank raises the
#: BLANK COLUMN warning and an Actions annotation rather than passing silently.
_COMPLETED_ON_KEYS: tuple[str, ...] = ("completedOn", "completedOnUtc")


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


#: ServiceTitan's documented customer object carries contact details as
#: ``contacts: [{id, type, value, memo}]`` with ``type`` one of
#: ``Phone | MobilePhone | Email | Fax``. Matched case-folded, mobile preferred
#: over landline. Fax is deliberately NOT a phone: the tab's `customer_phone` is
#: what somebody rings. The same tuples and the same selector as the contacts
#: ENDPOINT (``feeds/contacts.py``), so this fallback layer and the real source
#: can never disagree about which entry is the customer's phone number.
_PHONE_CONTACT_TYPES = PHONE_TYPES_IN_PREFERENCE_ORDER
_EMAIL_CONTACT_TYPES = EMAIL_TYPES_IN_PREFERENCE_ORDER


def _contact_detail(
    customer: dict[str, Any] | None,
    *,
    settings_key: str,
    fields: tuple[str, ...],
    contact_types: tuple[str, ...] = (),
) -> Any:
    """One customer phone/email, from every place ServiceTitan might put it.

    Three layers, tried in order, first non-empty wins:
    ``customer[settings_key][i][field]`` -> ``customer['contacts'][i]`` selected
    by ``type`` -> the flat ``customer[field]`` scalar. The middle layer is the
    one ServiceTitan's own customer schema documents (see ``_typed_contact``);
    the other two are prior readings kept because this contract only widens.

    ServiceTitan's CRM customer carries its contact details as
    ``phoneSettings: [{phone: ...}]`` / ``emailSettings: [{email: ...}]``. Profit
    Wizard's production client reads exactly those and treats the flat ``phone``
    / ``email`` scalars only as a fallback (``lib/crm/servicetitan.ts:903-906``),
    so reading only the scalars is the same shape of mistake that left
    ``job_number`` blank on every row ever exported — a column that is empty on
    every row, which reads as "this contractor has no phone numbers".

    Widen-only, so it cannot regress: the array is preferred when it has a
    usable entry and the old scalar behaviour is preserved underneath it. The
    tab has one cell, not a list, so the FIRST non-empty entry wins — that is
    the customer's primary number in ServiceTitan's own ordering.

    ``fields`` is a TUPLE of spellings for the same fact, tried in order on each
    entry, because the element's own field name is not verified against a live
    tenant: ServiceTitan's documented ``CustomerPhoneSettings`` looks like
    ``{phoneNumber, doNotText}`` while every fixture here says ``phone``. Reading
    only one of them is precisely how ``job_number`` stayed blank on every row
    ever exported. Reading all of them costs nothing and cannot be wrong — see
    ``KNOWN_UNVERIFIED.md``.
    """
    if not customer:
        return None
    for entry in customer.get(settings_key) or []:
        if not isinstance(entry, dict):
            continue
        for field in fields:
            value = entry.get(field)
            if value is not None and str(value).strip():
                return value
    contact_value = _typed_contact(customer, contact_types)
    if contact_value is not None:
        return contact_value
    return _first_present(customer, keys=fields)


def _typed_contact(customer: dict[str, Any], contact_types: tuple[str, ...]) -> Any:
    """First non-empty ``contacts[]`` entry of one of ``contact_types``.

    This is the shape ServiceTitan's own customer schema documents — ``id,
    active, name, type, address, contacts, balance, …`` with
    ``contacts[] {id, type, value, memo}`` and ``type`` in
    ``Phone | MobilePhone | Email | Fax``. That schema lists **none** of
    ``phone``/``phoneNumber``/``email``/``emailAddress`` on the customer, and no
    ``phoneSettings``/``emailSettings`` array either: `phoneSettings` appears to
    belong to the CONTACT record and to be an object rather than an array. So
    this is the reading most likely to be the real one.

    It is added BENEATH the settings-array and ABOVE the flat scalar rather than
    replacing either. **This is not purely widening, and the precedent matters.**
    A tenant carrying BOTH a typed ``contacts[]`` entry and a populated flat
    ``phone`` used to export the scalar and now exports the contact:

        {"contacts": [{"type": "Phone", "value": "A"}], "phone": "B"}

    gave ``B`` before and gives ``A`` now. That is deliberate — ``contacts[]`` is
    the shape ServiceTitan's schema actually documents, and the flat scalar is a
    guess kept as a fallback — but it IS a changed value for that shape, not a
    newly-filled blank. What the layering does guarantee is the weaker and more
    important property: no column that resolved a value before is blank now, and
    a tenant that only ever had ``contacts[]`` stops exporting a blank column.
    Selection is by ``type``, never by position — index 0 of a customer's contacts can just
    as easily be their fax number, and an email in the phone column is a wrong
    answer, which is worse than a blank one.

    Untyped entries are skipped for the same reason. An empty ``contact_types``
    means the caller is not asking about contacts at all.

    **This is the fallback, not the source.** The details really live on
    ``customers/{id}/contacts`` (``feeds/contacts.py``), which
    ``apply_customer_contacts`` overlays on top of whatever this resolved. This
    layer is kept because the contract only ever widens: a tenant that does carry
    a flat ``phone`` or a ``contacts[]`` array still exports it, and it is what
    fills the cell when the contacts endpoint is refused.
    """
    if not contact_types:
        return None
    return select_contact_value(customer.get("contacts") or [], contact_types)


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
                    # ServiceTitan's JPM job object names this `jobNumber`; reading
                    # only `number` left the column blank on every row ever exported
                    # (0 non-empty cells across 2431 live rows), which hard-blocked
                    # the consumer's NOT NULL `jobs.job_number`. `number` is kept as a
                    # fallback because the contract only ever widens, never narrows.
                    "job_number": _first_present(job, keys=("jobNumber", "number")),
                    "st_technician_id": technician_id,
                    "customer_name": customer.get("name") if customer else None,
                    "customer_phone": _contact_detail(
                        customer,
                        settings_key="phoneSettings",
                        fields=("phone", "phoneNumber", "number"),
                        contact_types=_PHONE_CONTACT_TYPES,
                    ),
                    "customer_email": _contact_detail(
                        customer,
                        settings_key="emailSettings",
                        fields=("email", "emailAddress"),
                        contact_types=_EMAIL_CONTACT_TYPES,
                    ),
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
                    # The JOB's own completion instant, never derived from the
                    # appointment. Profit Wizard's `jobs.completed_date` was null
                    # on all 864 completed jobs of the live tenant because this
                    # tab carried no completion timestamp at all, so every
                    # jobs-backed analytic that filters on it read the tenant as
                    # having done no work: the technicians roster showed named
                    # techs a 0% close rate and $0, and the Safety System
                    # reported "no completed jobs in the last 90 days".
                    #
                    # Deliberately NOT synthesised from `appointment_end`. A
                    # consumer can already do that fallback itself and several
                    # do; what none of them can do is tell a real completion
                    # apart from a guess. An appointment that ended is not a job
                    # that completed — a technician can finish a visit on a job
                    # that stays open for parts, a second visit or an approval,
                    # and the last appointment on a cancelled job ends too.
                    # Writing a guess into this column would make the guess
                    # indistinguishable from the fact for every consumer,
                    # permanently.
                    #
                    # Blank for a job ServiceTitan has not completed, which is
                    # correct and is what "blank is not zero" means here.
                    "completed_on": _first_present(job, keys=_COMPLETED_ON_KEYS),
                    # Not a contract column; used by run.py to sort deterministically
                    # without re-deriving ints from formatted text.
                    "_sort_key": _sort_key(job.get("id"), appointment_id),
                    # Also not a contract column (``build_job_grid`` renders
                    # ``JOB_COLUMNS`` and nothing else, so an extra key cannot reach
                    # a cell). It carries the join key the CONTACTS endpoint needs,
                    # so run.py can fetch contacts for exactly the customers whose
                    # rows survived the window filter rather than for every customer
                    # ever cached. ``apply_customer_contacts`` removes it.
                    "_customer_id": job.get("customerId"),
                }
            )

    rows.sort(key=lambda r: r["_sort_key"])
    for row in rows:
        del row["_sort_key"]
    return DenormalizeResult(rows=rows, skipped_no_job=skipped_no_job)


def customer_ids(rows: list[dict[str, Any]]) -> list[Any]:
    """The customer id behind each row, in row order, blanks dropped.

    Duplicated ids are kept as-is — deduping belongs to the fetcher, which is the
    thing that has to count requests (``feeds.contacts.fetch_contacts_per_customer``).
    """
    return [row["_customer_id"] for row in rows if row.get("_customer_id") not in (None, "")]


def apply_customer_contacts(
    rows: list[dict[str, Any]],
    contacts_by_customer: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Overlay the contacts endpoint's answer onto ``customer_phone``/``customer_email``.

    In place, and **it takes precedence**: the details really live on
    ``crm/v2/tenant/{id}/customers/{id}/contacts``, and the readers in
    ``_contact_detail`` are the widened fallback for a tenant that happens to
    carry them on the customer record too. Precedence only decides between two
    POPULATED values — a customer with no contact of that type, or a run where
    the contacts call was refused, keeps whatever the fallback resolved rather
    than having it blanked. That is the invariant worth stating: this can fill a
    blank cell, and it can change a value, but it can never empty one.

    Always removes the private ``_customer_id`` key, so calling it with an empty
    mapping (the degraded path) still leaves rows in exactly the shape the grid
    builder expects.
    """
    for row in rows:
        customer_id = row.pop("_customer_id", None)
        contacts = contacts_by_customer.get(str(customer_id)) if customer_id is not None else None
        if not contacts:
            continue
        phone = select_contact_value(contacts, _PHONE_CONTACT_TYPES)
        if phone is not None:
            row["customer_phone"] = phone
        email = select_contact_value(contacts, _EMAIL_CONTACT_TYPES)
        if email is not None:
            row["customer_email"] = email


def _sort_key(job_id: Any, appointment_id: Any) -> tuple[int, int]:
    def as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return (as_int(job_id), as_int(appointment_id))

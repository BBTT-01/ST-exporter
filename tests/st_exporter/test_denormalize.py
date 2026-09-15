from __future__ import annotations

from st_exporter.denormalize import build_job_rows
from st_exporter.feeds.raw_cache import RawCache


def _cache(*records: dict) -> RawCache:
    cache = RawCache()
    cache.merge(list(records))
    return cache


def test_basic_join_produces_one_row_per_appointment() -> None:
    jobs = _cache(
        {"id": 1, "jobNumber": "J-1", "customerId": 10, "locationId": 20, "jobStatus": "Scheduled"}
    )
    appointments = _cache(
        {
            "id": 100,
            "jobId": 1,
            "start": "2026-09-03T09:00:00-05:00",
            "end": "2026-09-03T11:00:00-05:00",
        }
    )
    assignments = _cache()
    customers = _cache(
        {"id": 10, "name": "Jane Doe", "phone": "555-1111", "email": "jane@example.com"}
    )
    locations = _cache(
        {
            "id": 20,
            "address": {
                "street": "123 Main St",
                "city": "Springfield",
                "state": "IL",
                "zip": "62701",
            },
        }
    )

    result = build_job_rows(jobs, appointments, assignments, customers, locations)

    assert result.skipped_no_job == 0
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["st_job_id"] == 1
    assert row["st_appointment_id"] == 100
    assert row["job_number"] == "J-1"
    assert row["customer_name"] == "Jane Doe"
    assert row["customer_phone"] == "555-1111"
    assert row["customer_email"] == "jane@example.com"
    assert row["service_address"] == "123 Main St, Springfield, IL, 62701"
    assert row["job_status"] == "Scheduled"
    assert row["appointment_start"] == "2026-09-03T09:00:00-05:00"
    assert row["st_technician_id"] is None


def test_appointment_with_no_matching_job_is_skipped_not_dropped_forever() -> None:
    jobs = _cache()  # job hasn't arrived in the raw cache yet
    appointments = _cache({"id": 100, "jobId": 999, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows == []
    assert result.skipped_no_job == 1


def test_unassigned_appointment_still_produces_a_row_with_blank_technician() -> None:
    jobs = _cache({"id": 1, "customerId": None, "locationId": None})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert len(result.rows) == 1
    assert result.rows[0]["st_technician_id"] is None


def test_active_assignment_resolves_the_technician() -> None:
    jobs = _cache({"id": 1})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    assignments = _cache(
        {
            "id": 1,
            "appointmentId": 100,
            "technicianId": 55,
            "status": "Active",
            "assignedOn": "2026-09-01T08:00:00-05:00",
        }
    )

    result = build_job_rows(jobs, appointments, assignments, _cache(), _cache())

    assert result.rows[0]["st_technician_id"] == "55"


def test_removed_assignment_does_not_count_as_active() -> None:
    jobs = _cache({"id": 1})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    assignments = _cache(
        {
            "id": 1,
            "appointmentId": 100,
            "technicianId": 55,
            "status": "Unassigned",
            "assignedOn": "2026-09-01T08:00:00-05:00",
        }
    )

    result = build_job_rows(jobs, appointments, assignments, _cache(), _cache())

    assert result.rows[0]["st_technician_id"] is None


def test_latest_assignment_wins_over_an_earlier_removed_one() -> None:
    jobs = _cache({"id": 1})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    assignments = _cache(
        {
            "id": 1,
            "appointmentId": 100,
            "technicianId": 10,
            "status": "Active",
            "assignedOn": "2026-09-01T08:00:00-05:00",
        },
        {
            "id": 2,
            "appointmentId": 100,
            "technicianId": 10,
            "status": "Unassigned",
            "assignedOn": "2026-09-02T08:00:00-05:00",
        },
        {
            "id": 3,
            "appointmentId": 100,
            "technicianId": 20,
            "status": "Active",
            "assignedOn": "2026-09-02T09:00:00-05:00",
        },
    )

    result = build_job_rows(jobs, appointments, assignments, _cache(), _cache())

    # Exactly one row: technician 10 still has a live "Active" record in the
    # append-only feed, and the fan-out must not resurrect them from it.
    assert len(result.rows) == 1
    assert result.rows[0]["st_technician_id"] == "20"


def test_concurrent_multi_technician_emits_a_row_each() -> None:
    jobs = _cache({"id": 1})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    assignments = _cache(
        {
            "id": 1,
            "appointmentId": 100,
            "technicianId": 30,
            "status": "Active",
            "assignedOn": "2026-09-01T08:00:00-05:00",
        },
        {
            "id": 2,
            "appointmentId": 100,
            "technicianId": 15,
            "status": "Active",
            "assignedOn": "2026-09-01T08:00:00-05:00",
        },
    )

    result = build_job_rows(jobs, appointments, assignments, _cache(), _cache())

    # Both technicians are genuinely assigned, so both get the job. The old
    # single-column contract had to discard one; the ordering survives only so
    # row order stays stable between runs.
    assert [r["st_technician_id"] for r in result.rows] == ["15", "30"]
    assert {r["st_appointment_id"] for r in result.rows} == {100}


def test_three_technician_crew_each_get_the_same_job() -> None:
    """A real install crew: one appointment, three live assignments.

    Modelled on ServiceTitan job 21465348 (Pioneer Overhead Door), where a
    three-technician install exported as a single row and the job was invisible
    to two of the three technicians.
    """
    jobs = _cache({"id": 1, "jobNumber": "J-1", "jobStatus": "InProgress"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-09T14:00:00Z"})
    assignments = _cache(
        {
            "id": 1,
            "appointmentId": 100,
            "technicianId": 2565,
            "status": "Active",
            "assignedOn": "2026-09-08T08:00:00Z",
        },
        {
            "id": 2,
            "appointmentId": 100,
            "technicianId": 20852220,
            "status": "Active",
            "assignedOn": "2026-09-08T09:00:00Z",
        },
        {
            "id": 3,
            "appointmentId": 100,
            "technicianId": 20364412,
            "status": "Active",
            "assignedOn": "2026-09-08T10:00:00Z",
        },
    )

    result = build_job_rows(jobs, appointments, assignments, _cache(), _cache())

    assert len(result.rows) == 3
    assert [r["st_technician_id"] for r in result.rows] == [
        "20364412",  # assigned last
        "20852220",
        "2565",
    ]
    # Every row is the same job and appointment — only the technician differs.
    assert {r["st_job_id"] for r in result.rows} == {1}
    assert {r["st_appointment_id"] for r in result.rows} == {100}
    assert {r["job_status"] for r in result.rows} == {"InProgress"}


def test_a_technician_reassigned_after_removal_is_present_exactly_once() -> None:
    """Assign -> unassign -> reassign. The latest event decides, and wins only once."""
    jobs = _cache({"id": 1})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00Z"})
    assignments = _cache(
        {
            "id": 1,
            "appointmentId": 100,
            "technicianId": 10,
            "status": "Active",
            "assignedOn": "2026-09-01T08:00:00Z",
        },
        {
            "id": 2,
            "appointmentId": 100,
            "technicianId": 10,
            "status": "Unassigned",
            "assignedOn": "2026-09-02T08:00:00Z",
        },
        {
            "id": 3,
            "appointmentId": 100,
            "technicianId": 10,
            "status": "Active",
            "assignedOn": "2026-09-03T08:00:00Z",
        },
    )

    result = build_job_rows(jobs, appointments, assignments, _cache(), _cache())

    assert [r["st_technician_id"] for r in result.rows] == ["10"]


def test_appointment_with_every_technician_removed_emits_one_unassigned_row() -> None:
    jobs = _cache({"id": 1})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00Z"})
    assignments = _cache(
        {
            "id": 1,
            "appointmentId": 100,
            "technicianId": 10,
            "status": "Active",
            "assignedOn": "2026-09-01T08:00:00Z",
        },
        {
            "id": 2,
            "appointmentId": 100,
            "technicianId": 10,
            "status": "Unassigned",
            "assignedOn": "2026-09-02T08:00:00Z",
        },
    )

    result = build_job_rows(jobs, appointments, assignments, _cache(), _cache())

    # The consumer skips a null technician; the row still carries the job so the
    # appointment does not silently vanish from the Export Store.
    assert len(result.rows) == 1
    assert result.rows[0]["st_technician_id"] is None


def test_missing_coordinates_are_blank_not_zero() -> None:
    jobs = _cache({"id": 1, "locationId": 20})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    locations = _cache({"id": 20, "address": {"street": "1 Main St"}})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), locations)

    assert result.rows[0]["latitude"] is None
    assert result.rows[0]["longitude"] is None


def test_zero_coordinate_is_preserved_not_treated_as_missing() -> None:
    jobs = _cache({"id": 1, "locationId": 20})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    locations = _cache({"id": 20, "address": {"latitude": 0.0, "longitude": 0.0}})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), locations)

    assert result.rows[0]["latitude"] == 0.0
    assert result.rows[0]["longitude"] == 0.0


def test_job_type_prefers_embedded_name_over_reference_lookup() -> None:
    jobs = _cache({"id": 1, "jobTypeName": "Repair", "jobTypeId": 5})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    job_types = {"5": {"id": 5, "name": "Should not be used"}}

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache(), job_types=job_types)

    assert result.rows[0]["job_type"] == "Repair"


def test_job_type_falls_back_to_reference_lookup_by_id() -> None:
    jobs = _cache({"id": 1, "jobTypeId": 5})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    job_types = {"5": {"id": 5, "name": "Installation"}}

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache(), job_types=job_types)

    assert result.rows[0]["job_type"] == "Installation"


def test_job_type_is_none_when_unresolvable() -> None:
    jobs = _cache({"id": 1})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["job_type"] is None


def test_business_unit_resolves_via_reference_lookup() -> None:
    jobs = _cache({"id": 1, "businessUnitId": 7})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    business_units = {"7": {"id": 7, "name": "Garage Doors"}}

    result = build_job_rows(
        jobs, appointments, _cache(), _cache(), _cache(), business_units=business_units
    )

    assert result.rows[0]["business_unit"] == "Garage Doors"


def test_rows_are_sorted_by_job_id_then_appointment_id() -> None:
    jobs = _cache({"id": 2}, {"id": 1})
    appointments = _cache(
        {"id": 200, "jobId": 2, "start": "2026-09-03T09:00:00-05:00"},
        {"id": 101, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"},
        {"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"},
    )

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    ordering = [(row["st_job_id"], row["st_appointment_id"]) for row in result.rows]
    assert ordering == [(1, 100), (1, 101), (2, 200)]


def test_cancelled_job_is_excluded_from_output() -> None:
    jobs = _cache({"id": 1, "jobStatus": "Canceled"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows == []


def test_cancelled_job_status_is_matched_case_insensitively() -> None:
    jobs = _cache({"id": 1, "jobStatus": "CANCELLED"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows == []


def test_non_cancelled_status_is_not_excluded() -> None:
    jobs = _cache({"id": 1, "jobStatus": "InProgress"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert len(result.rows) == 1


def test_technician_resolution_compares_assignedon_by_real_time_not_string() -> None:
    # A +02:00 timestamp can sort greater than a -05:00 timestamp lexicographically
    # even though the -05:00 one is chronologically later. Tech 20's event here is
    # 5 real hours after tech 10's, despite "…T10:00:00+02:00" > "…T08:00:00-05:00"
    # as raw strings.
    jobs = _cache({"id": 1})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    assignments = _cache(
        {
            "id": 1,
            "appointmentId": 100,
            "technicianId": 10,
            "status": "Active",
            "assignedOn": "2026-09-02T10:00:00+02:00",  # 08:00 UTC
        },
        {
            "id": 2,
            "appointmentId": 100,
            "technicianId": 20,
            "status": "Active",
            "assignedOn": "2026-09-02T08:00:00-05:00",  # 13:00 UTC — actually later
        },
    )

    result = build_job_rows(jobs, appointments, assignments, _cache(), _cache())

    assert result.rows[0]["st_technician_id"] == "20"


def test_unrelated_run_untouched_appointment_formats_identically() -> None:
    # Denormalisation itself is deterministic given identical raw input — this is
    # the guarantee run.py's "byte-identical unchanged rows" acceptance criterion
    # (tested at the run.py integration level) rests on.
    jobs = _cache({"id": 1, "jobNumber": "J-1"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    first = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())
    second = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert first.rows == second.rows


def test_job_number_comes_from_servicetitan_jobnumber_field() -> None:
    """A job payload in ServiceTitan's real shape must fill `job_number`.

    The JPM job object names the field `jobNumber`. The exporter read `number`,
    so the column was emitted but never filled — 0 non-empty cells across 2431
    live rows — and the consumer's NOT NULL `jobs.job_number` rejected every
    insert. Every job fixture in this suite used `number` too, so the whole
    suite passed vacuously. This test fails if anyone reads only `number` again.
    """
    jobs = _cache({"id": 1, "jobNumber": "JOB-4821", "jobStatus": "Scheduled"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert len(result.rows) == 1
    assert result.rows[0]["job_number"] == "JOB-4821"
    assert result.rows[0]["job_number"] not in (None, "")


def test_job_number_falls_back_to_legacy_number_field() -> None:
    """The contract only ever widens: a payload carrying the old `number`
    spelling (and no `jobNumber`) must still fill the column."""
    jobs = _cache({"id": 1, "number": "LEGACY-7", "jobStatus": "Scheduled"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["job_number"] == "LEGACY-7"


def test_job_number_prefers_jobnumber_when_both_spellings_are_present() -> None:
    jobs = _cache({"id": 1, "jobNumber": "REAL-1", "number": "STALE-1"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["job_number"] == "REAL-1"


def test_job_number_is_blank_only_when_servicetitan_sends_neither_spelling() -> None:
    jobs = _cache({"id": 1, "jobStatus": "Scheduled"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["job_number"] is None


def _rows_for_customer(customer: dict) -> dict:
    """One job row for a single customer record — the contact columns under test."""
    result = build_job_rows(
        _cache({"id": 1, "jobNumber": "J-1", "customerId": 10}),
        _cache({"id": 100, "jobId": 1}),
        _cache(),
        _cache(customer),
        _cache(),
    )
    return result.rows[0]


class TestCustomerContactColumns:
    """ServiceTitan carries contact details as settings ARRAYS, not scalars.

    Profit Wizard's production client reads `c.phoneSettings[].phone` /
    `c.emailSettings[].email` and treats the flat scalars only as a fallback
    (`lib/crm/servicetitan.ts:903-906`). Reading only the scalar is exactly the
    `job_number` failure again: a column blank on every row, which reads as "this
    contractor has no phone numbers" rather than as an error.
    """

    def test_the_settings_arrays_are_preferred(self) -> None:
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane Doe",
                "phoneSettings": [{"phone": "555-2222"}, {"phone": "555-3333"}],
                "emailSettings": [{"email": "jane@settings.test"}],
            }
        )
        # One cell, not a list: the first non-empty entry is ServiceTitan's own
        # primary ordering.
        assert row["customer_phone"] == "555-2222"
        assert row["customer_email"] == "jane@settings.test"

    def test_the_scalars_remain_the_fallback(self) -> None:
        # Widen-only, so it cannot regress: today's behaviour is preserved
        # underneath the array lookup.
        row = _rows_for_customer(
            {"id": 10, "name": "Jane", "phone": "555-1111", "email": "jane@flat.test"}
        )
        assert row["customer_phone"] == "555-1111"
        assert row["customer_email"] == "jane@flat.test"

    def test_an_empty_or_blank_settings_entry_falls_back_rather_than_blanking(self) -> None:
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane",
                "phoneSettings": [{"phone": ""}],
                "emailSettings": [],
                "phone": "555-1111",
                "email": "jane@flat.test",
            }
        )
        assert row["customer_phone"] == "555-1111"
        assert row["customer_email"] == "jane@flat.test"

    def test_the_documented_phone_number_spelling_is_read_too(self) -> None:
        """ServiceTitan's documented `CustomerPhoneSettings` is {phoneNumber, doNotText}.

        Which spelling a live tenant actually returns is UNVERIFIED
        (KNOWN_UNVERIFIED.md), and every other fixture here says `phone` — the
        exact arrangement that hid `job_number` for the life of that feature. So
        both are read, and both are tested.
        """
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane Doe",
                "phoneSettings": [{"phoneNumber": "555-4444", "doNotText": False}],
                "emailSettings": [{"emailAddress": "jane@documented.test"}],
            }
        )
        assert row["customer_phone"] == "555-4444"
        assert row["customer_email"] == "jane@documented.test"

    def test_a_bare_number_key_in_the_settings_entry_is_read(self) -> None:
        row = _rows_for_customer(
            {"id": 10, "name": "Jane", "phoneSettings": [{"number": "555-5555"}]}
        )
        assert row["customer_phone"] == "555-5555"

    def test_the_phone_spelling_still_wins_when_both_are_present(self) -> None:
        # Widen-only: adding spellings must not change what an existing tenant sees.
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane",
                "phoneSettings": [{"phone": "555-2222", "phoneNumber": "555-4444"}],
            }
        )
        assert row["customer_phone"] == "555-2222"

    def test_the_scalar_fallback_also_covers_the_alternative_spellings(self) -> None:
        row = _rows_for_customer({"id": 10, "name": "Jane", "phoneNumber": "555-6666"})
        assert row["customer_phone"] == "555-6666"

    def test_no_customer_at_all_is_still_none(self) -> None:
        result = build_job_rows(
            _cache({"id": 1, "jobNumber": "J-1", "customerId": 99}),
            _cache({"id": 100, "jobId": 1}),
            _cache(),
            _cache(),
            _cache(),
        )
        assert result.rows[0]["customer_phone"] is None


class TestCustomerContactsArray:
    """The shape ServiceTitan's own customer schema documents: `contacts[]`.

    The documented v2 customer object is
    `id, active, name, type, address, contacts, balance, doNotMail,
    doNotService, hasActiveMembership, memberships, customFields, createdOn,
    modifiedOn, mergedToId, externalData` — with
    `contacts[] {id, type ∈ Phone|MobilePhone|Email|Fax, value, memo}` and
    **none** of `phone`, `phoneNumber`, `email`, `emailAddress`,
    `phoneSettings` or `emailSettings` on the customer at all. If that is the
    real shape, every spelling the previous round widened to is read off an
    object that carries none of them and both columns are blank on every row —
    `job_number` again. So `contacts[]` is read too.
    """

    def test_a_phone_contact_is_read_when_nothing_else_is_there(self) -> None:
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane Doe",
                "contacts": [
                    {"id": 1, "type": "Phone", "value": "555-7777", "memo": "home"},
                    {"id": 2, "type": "Email", "value": "jane@contacts.test"},
                ],
            }
        )
        assert row["customer_phone"] == "555-7777"
        assert row["customer_email"] == "jane@contacts.test"

    def test_a_mobile_phone_counts_as_a_phone(self) -> None:
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane",
                "contacts": [{"id": 1, "type": "MobilePhone", "value": "555-8888"}],
            }
        )
        assert row["customer_phone"] == "555-8888"

    def test_the_type_is_matched_case_insensitively(self) -> None:
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane",
                "contacts": [{"type": "phone", "value": "555-9999"}],
            }
        )
        assert row["customer_phone"] == "555-9999"

    def test_a_fax_is_not_a_phone_number(self) -> None:
        """Selection is by `type`, never by position — the phone column is what
        somebody rings, and a wrong number is worse than a blank one."""
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane",
                "contacts": [
                    {"type": "Fax", "value": "555-0000"},
                    {"type": "Phone", "value": "555-1234"},
                ],
            }
        )
        assert row["customer_phone"] == "555-1234"

    def test_an_email_never_lands_in_the_phone_column(self) -> None:
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane",
                "contacts": [{"type": "Email", "value": "jane@contacts.test"}],
            }
        )
        assert row["customer_phone"] is None
        assert row["customer_email"] == "jane@contacts.test"

    def test_an_untyped_contact_is_skipped_rather_than_guessed_at(self) -> None:
        row = _rows_for_customer({"id": 10, "name": "Jane", "contacts": [{"value": "555-????"}]})
        assert row["customer_phone"] is None

    def test_a_blank_contact_value_falls_through_to_the_next_entry(self) -> None:
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane",
                "contacts": [
                    {"type": "Phone", "value": ""},
                    {"type": "Phone", "value": "555-2468"},
                ],
            }
        )
        assert row["customer_phone"] == "555-2468"

    def test_contacts_sit_above_the_flat_scalar_and_below_the_settings_array(self) -> None:
        """Widen-only: the new layer is INSERTED, so no shape that resolved a
        value before resolves a different one now."""
        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane",
                "phoneSettings": [{"phone": "from-settings"}],
                "contacts": [{"type": "Phone", "value": "from-contacts"}],
                "phone": "from-scalar",
            }
        )
        assert row["customer_phone"] == "from-settings"

        row = _rows_for_customer(
            {
                "id": 10,
                "name": "Jane",
                "contacts": [{"type": "Phone", "value": "from-contacts"}],
                "phone": "from-scalar",
            }
        )
        assert row["customer_phone"] == "from-contacts"

    def test_a_customer_carrying_only_the_documented_schema_is_not_blank(self) -> None:
        """The whole point: if `contacts[]` is the only real shape, neither
        column is blank across the whole tab."""
        row = _rows_for_customer(
            {
                "id": 10,
                "active": True,
                "name": "Jane Doe",
                "type": "Residential",
                "address": {"street": "1 Main St"},
                "contacts": [
                    {"id": 1, "type": "MobilePhone", "value": "555-3141", "memo": None},
                    {"id": 2, "type": "Email", "value": "jane@doe.test", "memo": None},
                    {"id": 3, "type": "Fax", "value": "555-0000", "memo": None},
                ],
                "balance": 0,
                "createdOn": "2026-01-01T00:00:00Z",
            }
        )
        assert row["customer_phone"] == "555-3141"
        assert row["customer_email"] == "jane@doe.test"

    def test_garbage_in_contacts_does_not_crash_the_feed(self) -> None:
        row = _rows_for_customer(
            {"id": 10, "name": "Jane", "contacts": ["not-a-dict", None, {"type": "Phone"}]}
        )
        assert row["customer_phone"] is None

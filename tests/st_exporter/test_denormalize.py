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
        """The new layer is INSERTED between the two that existed.

        NOT "no shape resolves a different value now" — that was wrong, and the
        second case below is the counter-example: contacts[] + a populated flat
        scalar used to give the scalar and now gives the contact. Deliberate
        (contacts[] is the documented shape; the scalar is a guess), but a changed
        value, not a filled blank. The property that does hold: nothing that
        resolved a value before is blank now.
        """
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


# --- completed_on --------------------------------------------------------------
#
# Profit Wizard's `jobs.completed_date` was null on all 864 completed jobs of the
# live tenant because the tab carried no completion timestamp at all. Everything
# that filters on it read the tenant as having done no work: named technicians
# shown a 0% close rate and $0, and "no completed jobs in the last 90 days" while
# 864 sat inside the window. These pin the column at the producer end, which is
# the only end this repo owns — and, because an APPENDED column has no committed
# contract fixture (see `st_exporter.contracts`), they are the only fixture-like
# cover it has here. The live end is covered by `blank_columns`, which reports a
# column empty on every row.


def test_completed_on_comes_from_the_servicetitan_completion_timestamp() -> None:
    jobs = _cache(
        {
            "id": 1,
            "jobNumber": "J-1",
            "jobStatus": "Completed",
            "completedOn": "2026-09-03T16:30:00-05:00",
        }
    )
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["completed_on"] == "2026-09-03T16:30:00-05:00"


def test_completed_on_accepts_the_alternate_spelling() -> None:
    """The contract only ever widens — the same reason `job_number` still reads
    `number`. Both names mean the same fact, so accepting both cannot pick up a
    different one."""
    jobs = _cache({"id": 1, "jobStatus": "Completed", "completedOnUtc": "2026-09-03T21:30:00Z"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["completed_on"] == "2026-09-03T21:30:00Z"


def test_completed_on_prefers_the_documented_spelling_when_both_are_present() -> None:
    jobs = _cache(
        {
            "id": 1,
            "completedOn": "2026-09-03T16:30:00-05:00",
            "completedOnUtc": "1999-01-01T00:00:00Z",
        }
    )
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["completed_on"] == "2026-09-03T16:30:00-05:00"


def test_completed_on_is_blank_for_a_job_servicetitan_has_not_completed() -> None:
    """Blank means "not completed", and blank is not zero. A scheduled job must
    not acquire a completion date."""
    jobs = _cache({"id": 1, "jobNumber": "J-1", "jobStatus": "Scheduled"})
    appointments = _cache(
        {
            "id": 100,
            "jobId": 1,
            "start": "2026-09-03T09:00:00-05:00",
            "end": "2026-09-03T11:00:00-05:00",
        }
    )

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["completed_on"] is None


def test_completed_on_is_never_synthesised_from_the_appointment_end() -> None:
    """The point of the column is a TRUE completion instant.

    A consumer can already fall back to `appointment_end` itself, and several do;
    what none of them can do is tell a real completion apart from a guess once
    the guess has been written into the column. An appointment that ended is not
    a job that completed — a visit can finish on a job that stays open for parts,
    a second visit or an approval.
    """
    jobs = _cache({"id": 1, "jobStatus": "InProgress"})
    appointments = _cache(
        {
            "id": 100,
            "jobId": 1,
            "start": "2026-09-03T09:00:00-05:00",
            "end": "2026-09-03T11:00:00-05:00",
        }
    )

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    row = result.rows[0]
    assert row["appointment_end"] == "2026-09-03T11:00:00-05:00"
    assert row["completed_on"] is None


def test_completed_on_is_a_job_fact_repeated_on_every_technician_row() -> None:
    """The tab's grain is one row per (appointment, technician); completion is a
    property of the JOB, so a crew of two gets the same instant on both rows —
    like `job_status` and `business_unit` beside it."""
    jobs = _cache({"id": 1, "jobStatus": "Completed", "completedOn": "2026-09-03T16:30:00-05:00"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
    assignments = _cache(
        {"id": 1, "appointmentId": 100, "technicianId": 900, "status": "Active"},
        {"id": 2, "appointmentId": 100, "technicianId": 901, "status": "Active"},
    )

    result = build_job_rows(jobs, appointments, assignments, _cache(), _cache())

    assert len(result.rows) == 2
    assert {row["completed_on"] for row in result.rows} == {"2026-09-03T16:30:00-05:00"}


def test_job_number_is_derived_from_the_job_number_not_copied_from_the_id() -> None:
    """`job_number` and `st_job_id` are two different facts read from two
    different keys, and a tenant where they differ must show the difference.

    On `tr-doorservpro` these two columns are EQUAL on all 2458 exported rows,
    which looks exactly like the column being copied from the id. It is not: QA
    corroborated it independently from a different endpoint — invoice
    `ReferenceNumber` equals `JobId` on 2815 of 2817 rows — so in that tenant
    ServiceTitan genuinely issues job numbers that match job ids.

    This test is what keeps that a coincidence rather than an implementation.
    `job_number` is the key `reporting.jobCosts` joins on (`JobNumber`), so a
    tenant whose numbers and ids diverge would mis-join every cost row to the
    wrong job — silently, since both columns would still be populated and both
    would still look like plausible ids.
    """
    jobs = _cache({"id": 4821, "jobNumber": "1007", "jobStatus": "Completed"})
    appointments = _cache({"id": 100, "jobId": 4821, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    row = result.rows[0]
    assert row["st_job_id"] == 4821
    assert row["job_number"] == "1007"
    assert str(row["job_number"]) != str(row["st_job_id"])


# --- total_revenue -------------------------------------------------------------
#
# `total_revenue` is 0 on all 1701 hosted jobs while the direct baseline carries
# it on 446, so every margin and profitability surface in Profit Wizard is empty.
# The source here is the DIRECT path's own — PW fills the column from
# `(job.total || job.invoiceTotal)` (lib/crm/servicetitan.ts:856) and that is the
# only thing in PW that writes it — so the two paths produce the same figure for
# the same job, which is what makes the QA comparison meaningful.


def test_total_revenue_comes_from_the_job_total() -> None:
    jobs = _cache({"id": 1, "jobStatus": "Completed", "total": 1250.50})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["total_revenue"] == 1250.50


def test_total_revenue_falls_back_to_invoice_total() -> None:
    """The second half of the direct path's `job.total || job.invoiceTotal`."""
    jobs = _cache({"id": 1, "jobStatus": "Completed", "invoiceTotal": 980})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["total_revenue"] == 980


def test_total_revenue_prefers_total_over_invoice_total() -> None:
    jobs = _cache({"id": 1, "total": 1250.50, "invoiceTotal": 999})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["total_revenue"] == 1250.50


def test_a_zero_total_is_treated_as_absent_not_as_free_work() -> None:
    """The correction. `0` here means "no revenue recorded", not "$0".

    The first cut of this column read `0` as a real zero, on the contract's
    "blank is not zero" rule. The live tenant disproved it: across 1256 rows of
    `tr-doorservpro`'s jobs tab NO row was blank, 45% of distinct jobs read
    exactly 0, and 41% of COMPLETED jobs reported $0 — ServiceTitan sends 0
    rather than null for "nothing recorded", so reading it literally labels four
    jobs in ten as free work.

    A blank makes Profit Wizard REFUSE to compute a margin; a 0 makes it compute
    one against zero revenue, i.e. -100%. And the blank-column tripwire cannot
    catch it, because it only fires on a column empty on EVERY row.
    """
    jobs = _cache({"id": 1, "jobStatus": "Completed", "total": 0})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["total_revenue"] is None


def test_a_zero_total_falls_through_to_the_invoice_total() -> None:
    """Mirrors the direct path's `||`: a 0 does not stop the search. Profit
    Wizard's `(job.total || job.invoiceTotal)` makes exactly this choice, which
    is why the direct baseline carries NULLs where hosted was writing zeros."""
    jobs = _cache({"id": 1, "jobStatus": "Completed", "total": 0, "invoiceTotal": 500})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["total_revenue"] == 500


def test_zero_on_both_spellings_is_blank_not_zero() -> None:
    jobs = _cache({"id": 1, "jobStatus": "Completed", "total": 0, "invoiceTotal": 0})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["total_revenue"] is None


def test_the_zero_rule_is_scoped_to_revenue_and_nothing_else() -> None:
    """`_money_or_absent` is narrow on purpose. A 0 cost or a 0 price elsewhere
    in this export is a real fact, and "blank is not zero" still holds
    everywhere it has not been overridden with live evidence."""
    from st_exporter.denormalize import _first_present, _money_or_absent

    record = {"a": 0, "b": 7}
    assert _first_present(record, keys=("a", "b")) == 0
    assert _money_or_absent(record, keys=("a", "b")) == 7


def test_only_a_numeric_zero_is_treated_as_absent() -> None:
    """A string is handed on rather than judged — parsing is the consumer's job,
    and `False` is not a price."""
    from st_exporter.denormalize import _money_or_absent

    assert _money_or_absent({"a": "0"}, keys=("a",)) == "0"
    assert _money_or_absent({"a": False, "b": 12}, keys=("a", "b")) == 12


def test_superseded_a_zero_dollar_job_exports_zero_not_blank() -> None:
    """Blank is not zero, and this is where the two part company with Profit
    Wizard's own expression.

    PW uses `job.total || job.invoiceTotal`, so a genuine `0` total is falsy and
    falls through to `invoiceTotal`. This exporter treats `0` as a value, because
    a zero-dollar job — a warranty callback, a goodwill visit — is a real fact
    and the contract is explicit that a blank money cell means ABSENT. Collapsing
    the two is the same error as turning missing cost into free work.
    """
    jobs = _cache({"id": 1, "jobStatus": "Completed", "total": 0, "invoiceTotal": 500})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    # SUPERSEDED by the live measurement above: this used to assert `== 0`.
    assert result.rows[0]["total_revenue"] == 500


def test_total_revenue_is_blank_when_servicetitan_records_none() -> None:
    jobs = _cache({"id": 1, "jobStatus": "Scheduled"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert result.rows[0]["total_revenue"] is None


def test_no_job_ever_reaches_the_sheet_carrying_a_zero_revenue_cell() -> None:
    """The distinction that matters has to survive into the SHEET, not just the
    row dict, because `""` and `"0"` are what a consumer actually sees and PW's
    `toNum` maps the first to null and the second to a real 0.

    This test used to assert the opposite — `["0", ""]` — and that assertion is
    exactly what shipped 503 completed jobs to a live customer reading $0. A
    zero-dollar cell is now never written at all: real money formats as itself,
    and both "recorded as 0" and "not recorded" format blank, which is what makes
    Profit Wizard refuse to compute a margin instead of computing -100%.
    """
    from st_exporter.format import JOB_COLUMNS, build_job_grid

    jobs = _cache(
        {"id": 1, "total": 1250.5, "jobStatus": "Completed"},
        {"id": 2, "total": 0, "jobStatus": "Completed"},
        {"id": 3, "jobStatus": "Scheduled"},
    )
    appointments = _cache(
        {"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"},
        {"id": 200, "jobId": 2, "start": "2026-09-03T09:00:00-05:00"},
        {"id": 300, "jobId": 3, "start": "2026-09-03T09:00:00-05:00"},
    )

    result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())
    grid = build_job_grid(result.rows)
    column = JOB_COLUMNS.index("total_revenue")
    cells = [row[column] for row in grid[1:]]

    assert cells == ["1250.5", "", ""]
    assert "0" not in cells


class TestHostedParityJobColumns:
    """The 6 columns appended to `jobs`, after `completed_on`/`total_revenue`, for
    Profit Wizard hosted parity.

    Read straight off the JOB record and never defaulted — a job that never
    completed, was never a recall, carries no warranty, or was never marked
    no-charge must stay ``None`` (blank), never a guessed ``"0"``/``"false"``.
    """

    def _row(self, job: dict) -> dict:
        jobs = _cache({"id": 1, "jobNumber": "J-1", **job})
        appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
        result = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())
        return result.rows[0]

    def test_all_six_populate_from_the_job_record(self) -> None:
        row = self._row(
            {
                "recallForId": 2,
                "warrantyId": 9,
                "noCharge": False,
                "total": 1250.5,
                "businessUnitId": 3,
                "soldById": 901,
            }
        )
        assert row["recall_for_id"] == 2
        assert row["warranty_id"] == 9
        assert row["no_charge"] is False
        assert row["total"] == 1250.5
        assert row["business_unit_id"] == 3
        assert row["sold_by_id"] == 901

    def test_all_six_are_blank_when_the_job_carries_none_of_them(self) -> None:
        row = self._row({})
        for column in (
            "recall_for_id",
            "warranty_id",
            "no_charge",
            "total",
            "business_unit_id",
            "sold_by_id",
        ):
            assert row[column] is None

    def test_no_charge_false_is_not_the_same_as_absent(self) -> None:
        """A real ``False`` must survive as ``False``, not collapse to blank —
        `to_cell_text` renders it `"false"`; only an absent field is blank."""
        row = self._row({"noCharge": False})
        assert row["no_charge"] is False

    def test_a_real_zero_total_is_not_blank(self) -> None:
        row = self._row({"total": 0})
        assert row["total"] == 0
        assert row["total"] is not None


class TestBookingIdJobColumn:
    def _grid_cells(self, *jobs: dict) -> list[str]:
        from st_exporter.format import JOB_COLUMNS, build_job_grid

        appointments = _cache(
            *(
                {"id": job["id"] * 100, "jobId": job["id"], "start": "2026-09-03T09:00:00-05:00"}
                for job in jobs
            )
        )
        result = build_job_rows(_cache(*jobs), appointments, _cache(), _cache(), _cache())
        grid = build_job_grid(result.rows)
        column = JOB_COLUMNS.index("booking_id")
        return [row[column] for row in grid[1:]]

    def test_populated_from_the_jobs_booking_id(self) -> None:
        assert self._grid_cells({"id": 115409266, "bookingId": 115382913}) == ["115382913"]

    def test_blank_when_servicetitan_sends_null(self) -> None:
        assert self._grid_cells({"id": 115493106, "bookingId": None}) == [""]

    def test_blank_when_the_field_is_absent(self) -> None:
        assert self._grid_cells({"id": 1}) == [""]

    def test_an_unbooked_job_is_never_zero_or_a_placeholder(self) -> None:
        cells = self._grid_cells(
            {"id": 1, "bookingId": 115382913},
            {"id": 2, "bookingId": None},
            {"id": 3},
        )
        assert cells == ["115382913", "", ""]
        assert "0" not in cells

    def test_every_technician_row_of_a_booked_job_carries_the_booking(self) -> None:
        jobs = _cache({"id": 1, "bookingId": 115382913})
        appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})
        assignments = _cache(
            {"id": 1, "appointmentId": 100, "technicianId": 7, "status": "Active"},
            {"id": 2, "appointmentId": 100, "technicianId": 8, "status": "Active"},
        )
        result = build_job_rows(jobs, appointments, assignments, _cache(), _cache())
        assert [row["booking_id"] for row in result.rows] == [115382913, 115382913]

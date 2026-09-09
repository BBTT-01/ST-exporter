from __future__ import annotations

from st_exporter.denormalize import build_job_rows
from st_exporter.feeds.raw_cache import RawCache


def _cache(*records: dict) -> RawCache:
    cache = RawCache()
    cache.merge(list(records))
    return cache


def test_basic_join_produces_one_row_per_appointment() -> None:
    jobs = _cache(
        {"id": 1, "number": "J-1", "customerId": 10, "locationId": 20, "jobStatus": "Scheduled"}
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
    jobs = _cache({"id": 1, "number": "J-1", "jobStatus": "InProgress"})
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
    jobs = _cache({"id": 1, "number": "J-1"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    first = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())
    second = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert first.rows == second.rows

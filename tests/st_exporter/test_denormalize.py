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

    assert result.rows[0]["st_technician_id"] == "20"


def test_concurrent_multi_technician_tie_breaks_on_lowest_technician_id() -> None:
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

    assert result.rows[0]["st_technician_id"] == "15"


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


def test_unrelated_run_untouched_appointment_formats_identically() -> None:
    # Denormalisation itself is deterministic given identical raw input — this is
    # the guarantee run.py's "byte-identical unchanged rows" acceptance criterion
    # (tested at the run.py integration level) rests on.
    jobs = _cache({"id": 1, "number": "J-1"})
    appointments = _cache({"id": 100, "jobId": 1, "start": "2026-09-03T09:00:00-05:00"})

    first = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())
    second = build_job_rows(jobs, appointments, _cache(), _cache(), _cache())

    assert first.rows == second.rows

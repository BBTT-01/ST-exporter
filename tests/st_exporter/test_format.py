from __future__ import annotations

from st_exporter.format import (
    JOB_COLUMNS,
    TECHNICIAN_COLUMNS,
    build_service_address,
    build_technician_grid,
    format_job_row,
    format_technician_row,
    technician_row,
    to_cell_text,
)


def test_none_becomes_blank_not_the_string_none() -> None:
    assert to_cell_text(None) == ""


def test_booleans_are_lowercase_json_style() -> None:
    assert to_cell_text(True) == "true"
    assert to_cell_text(False) == "false"


def test_zero_is_not_blank() -> None:
    # Distinguishing "no coordinate" (None) from a legitimate 0.0 is denormalize.py's
    # job, but format.py must not collapse the two on its own.
    assert to_cell_text(0) == "0"
    assert to_cell_text(0.0) == "0.0"


def test_strings_and_numbers_pass_through_as_text() -> None:
    assert to_cell_text("2026-09-03T10:00:00-05:00") == "2026-09-03T10:00:00-05:00"
    assert to_cell_text(40.7128) == "40.7128"
    assert to_cell_text(12345) == "12345"


def test_service_address_joins_present_parts_only() -> None:
    full = {
        "street": "123 Main St",
        "unit": "Apt 4",
        "city": "Springfield",
        "state": "IL",
        "zip": "62701",
    }
    assert build_service_address(full) == "123 Main St, Apt 4, Springfield, IL, 62701"


def test_service_address_skips_missing_unit_without_dangling_separator() -> None:
    no_unit = {"street": "123 Main St", "city": "Springfield", "state": "IL", "zip": "62701"}
    assert build_service_address(no_unit) == "123 Main St, Springfield, IL, 62701"


def test_service_address_handles_none() -> None:
    assert build_service_address(None) == ""
    assert build_service_address({}) == ""


def test_format_job_row_matches_column_order_exactly() -> None:
    row = {col: f"v-{col}" for col in JOB_COLUMNS}
    assert format_job_row(row) == [f"v-{col}" for col in JOB_COLUMNS]


def test_format_job_row_blanks_missing_keys() -> None:
    assert format_job_row({}) == [""] * len(JOB_COLUMNS)


def test_format_technician_row_matches_column_order_exactly() -> None:
    row = {
        "st_technician_id": "42",
        "name": "Jane Doe",
        "email": "jane@example.com",
        "active": True,
        "phone": "555-0100",
        "business_unit_id": "3",
        "business_unit_name": "Doors",
        "role_ids": "10,11",
        "home_address": "9 Fixture Ln, Springfield, IL, 62701",
        "home_latitude": 39.79,
        "home_longitude": -89.65,
    }
    assert format_technician_row(row) == [
        "42",
        "Jane Doe",
        "jane@example.com",
        "true",
        "555-0100",
        "3",
        "Doors",
        "10,11",
        "9 Fixture Ln, Springfield, IL, 62701",
        "39.79",
        "-89.65",
    ]
    assert TECHNICIAN_COLUMNS == (
        "st_technician_id",
        "name",
        "email",
        "active",
        "phone",
        "business_unit_id",
        "business_unit_name",
        "role_ids",
        "home_address",
        "home_latitude",
        "home_longitude",
    )


def test_job_columns_end_with_the_appended_hosted_parity_columns() -> None:
    """`completed_on`/`total_revenue` were appended first; these six follow them."""
    assert JOB_COLUMNS[-8:] == (
        "completed_on",
        "total_revenue",
        "recall_for_id",
        "warranty_id",
        "no_charge",
        "total",
        "business_unit_id",
        "sold_by_id",
    )


class TestTechnicianRowHostedParityColumns:
    def test_phone_widens_from_phonenumber_then_phone(self) -> None:
        assert technician_row({"id": 1, "phoneNumber": "555-0100"})["phone"] == "555-0100"
        assert technician_row({"id": 1, "phone": "555-0200"})["phone"] == "555-0200"
        assert technician_row({"id": 1})["phone"] is None

    def test_business_unit_name_resolves_against_the_same_reference_table_as_jobs(self) -> None:
        row = technician_row({"id": 1, "businessUnitId": 3}, {"3": {"name": "Doors"}})
        assert row["business_unit_id"] == 3
        assert row["business_unit_name"] == "Doors"

    def test_business_unit_name_is_blank_when_the_reference_lookup_is_unavailable(self) -> None:
        row = technician_row({"id": 1, "businessUnitId": 3})
        assert row["business_unit_id"] == 3
        assert row["business_unit_name"] is None

    def test_role_ids_are_comma_joined(self) -> None:
        assert technician_row({"id": 1, "roleIds": [10, 11]})["role_ids"] == "10,11"

    def test_role_ids_are_blank_when_absent_or_empty(self) -> None:
        assert technician_row({"id": 1})["role_ids"] is None
        assert technician_row({"id": 1, "roleIds": []})["role_ids"] is None

    def test_home_address_and_coordinates_read_the_homeaddress_object(self) -> None:
        row = technician_row(
            {
                "id": 1,
                "homeAddress": {
                    "street": "9 Fixture Ln",
                    "city": "Springfield",
                    "state": "IL",
                    "zip": "62701",
                    "latitude": 39.79,
                    "longitude": -89.65,
                },
            }
        )
        assert row["home_address"] == "9 Fixture Ln, Springfield, IL, 62701"
        assert row["home_latitude"] == 39.79
        assert row["home_longitude"] == -89.65

    def test_home_address_widens_to_the_home_key(self) -> None:
        row = technician_row({"id": 1, "home": {"street": "1 Elm St"}})
        assert row["home_address"] == "1 Elm St"

    def test_home_fields_are_blank_when_absent(self) -> None:
        row = technician_row({"id": 1})
        assert row["home_address"] == ""
        assert row["home_latitude"] is None
        assert row["home_longitude"] is None

    def test_a_zero_home_coordinate_is_preserved_not_treated_as_missing(self) -> None:
        row = technician_row({"id": 1, "homeAddress": {"latitude": 0, "longitude": 0}})
        assert row["home_latitude"] == 0
        assert row["home_longitude"] == 0


def test_build_technician_grid_threads_business_units_through_every_row() -> None:
    grid = build_technician_grid(
        [{"id": 1, "name": "Tech One", "businessUnitId": 3}], {"3": {"name": "Doors"}}
    )
    row = dict(zip(grid[0], grid[1]))
    assert row["business_unit_name"] == "Doors"

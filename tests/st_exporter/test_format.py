from __future__ import annotations

from st_exporter.format import (
    JOB_COLUMNS,
    TECHNICIAN_COLUMNS,
    build_service_address,
    format_job_row,
    format_technician_row,
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
    }
    assert format_technician_row(row) == ["42", "Jane Doe", "jane@example.com", "true"]
    assert TECHNICIAN_COLUMNS == ("st_technician_id", "name", "email", "active")

from __future__ import annotations

from st_exporter.meta import (
    META_COLUMNS,
    CursorBundle,
    MetaRow,
    build_meta_grid,
    parse_meta_grid,
)


def test_cursor_bundle_decode_of_empty_string_gives_all_none_tokens() -> None:
    bundle = CursorBundle.decode("")
    assert bundle.get("customers") is None
    assert bundle.get("jobs") is None


def test_cursor_bundle_decode_of_none_gives_all_none_tokens() -> None:
    bundle = CursorBundle.decode(None)
    assert bundle.get("assignments") is None


def test_cursor_bundle_round_trips_through_encode_decode() -> None:
    bundle = CursorBundle(
        {
            "customers": "tok_c",
            "locations": "tok_l",
            "jobs": "tok_j",
            "appointments": "tok_a",
            "assignments": "tok_s",
        }
    )
    decoded = CursorBundle.decode(bundle.encode())
    assert decoded.get("customers") == "tok_c"
    assert decoded.get("jobs") == "tok_j"
    assert decoded.get("assignments") == "tok_s"


def test_cursor_bundle_decode_of_malformed_json_gives_all_none_tokens() -> None:
    bundle = CursorBundle.decode("not json")
    assert bundle.get("jobs") is None


def test_cursor_bundle_encode_is_deterministic() -> None:
    a = CursorBundle({"jobs": "1", "customers": "2"}).encode()
    b = CursorBundle({"customers": "2", "jobs": "1"}).encode()
    assert a == b


def test_with_token_does_not_mutate_original() -> None:
    original = CursorBundle({"jobs": "old"})
    updated = original.with_token("jobs", "new")
    assert original.get("jobs") == "old"
    assert updated.get("jobs") == "new"


def test_build_meta_grid_has_header_and_is_sorted_by_feed() -> None:
    rows = [
        MetaRow(feed="technicians", last_run_at="t2", row_count=4, exporter_version="0.1.0"),
        MetaRow(
            feed="jobs", last_run_at="t1", last_cursor="{}", row_count=10, exporter_version="0.1.0"
        ),
    ]
    grid = build_meta_grid(rows)
    assert grid[0] == list(META_COLUMNS)
    assert grid[1][0] == "jobs"
    assert grid[2][0] == "technicians"
    assert grid[1] == ["jobs", "t1", "{}", "10", "0.1.0"]


def test_parse_meta_grid_round_trips_build_meta_grid() -> None:
    rows = [
        MetaRow(
            feed="jobs", last_run_at="t1", last_cursor="{}", row_count=10, exporter_version="0.1.0"
        )
    ]
    grid = build_meta_grid(rows)
    parsed = parse_meta_grid(grid)
    assert parsed["jobs"].row_count == 10
    assert parsed["jobs"].last_cursor == "{}"
    assert parsed["jobs"].exporter_version == "0.1.0"


def test_parse_meta_grid_of_empty_grid_is_empty() -> None:
    assert parse_meta_grid([]) == {}


def test_parse_meta_grid_skips_blank_rows() -> None:
    grid = [list(META_COLUMNS), ["", "", "", "", ""]]
    assert parse_meta_grid(grid) == {}

from __future__ import annotations

import pytest

from st_exporter.meta import (
    META_COLUMNS,
    CursorBundle,
    MetaRow,
    MetaRowSet,
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


def test_cursor_bundle_decode_of_json_list_gives_all_none_tokens() -> None:
    # Valid JSON, but not an object — a hand-edited _meta cell. Must not crash
    # (data.get(...) on a list raises AttributeError) at the top of every run.
    bundle = CursorBundle.decode("[1, 2, 3]")
    assert bundle.get("jobs") is None


def test_cursor_bundle_decode_of_json_string_gives_all_none_tokens() -> None:
    bundle = CursorBundle.decode('"just a string"')
    assert bundle.get("jobs") is None


def test_cursor_bundle_decode_of_json_number_gives_all_none_tokens() -> None:
    bundle = CursorBundle.decode("5")
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
    # Trailing "" is contract_version: MetaRow defaults it to blank, which is what
    # an exporter of 0.2.8 or older wrote. Every feed declares a real one now (see
    # st_exporter.contracts); this test is about the grid shape, not the value.
    assert grid[1] == ["jobs", "t1", "{}", "10", "0.1.0", ""]


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
    grid = [list(META_COLUMNS), ["", "", "", "", "", ""]]
    assert parse_meta_grid(grid) == {}


def test_meta_row_round_trips_contract_version() -> None:
    rows = [
        MetaRow(
            feed="pricebook.services",
            last_run_at="t1",
            row_count=2,
            exporter_version="0.2.8",
            contract_version="pricebook.v1",
        )
    ]
    parsed = parse_meta_grid(build_meta_grid(rows))
    assert parsed["pricebook.services"].contract_version == "pricebook.v1"
    assert parsed["pricebook.services"].last_cursor == ""


def test_parse_meta_grid_of_a_pre_contract_version_grid_reads_it_as_blank() -> None:
    # A _meta tab written by an older exporter has no contract_version column at
    # all. "missing column" must read back as blank, not crash.
    grid = [
        ["feed", "last_run_at", "last_cursor", "row_count", "exporter_version"],
        ["jobs", "t1", "{}", "10", "0.1.0"],
    ]
    assert parse_meta_grid(grid)["jobs"].contract_version == ""


def test_parse_meta_grid_tolerates_non_numeric_row_count() -> None:
    # A hand-edited or corrupted _meta cell must not crash the run before any
    # work happens — mirrors CursorBundle.decode's tolerance of bad data.
    grid = [list(META_COLUMNS), ["jobs", "t1", "{}", "N/A", "0.1.0"]]
    parsed = parse_meta_grid(grid)
    assert parsed["jobs"].row_count == 0


def test_parse_meta_grid_row_count_blank_cell_is_zero() -> None:
    grid = [list(META_COLUMNS), ["jobs", "t1", "{}", "", "0.1.0"]]
    assert parse_meta_grid(grid)["jobs"].row_count == 0


class TestOneRowPerTab:
    """`_meta` is parsed last-wins, so a duplicated tab does not read downstream
    as an error — it reads as the WRONG `last_run_at`, on a tab that was just
    refreshed, quietly, forever. `docs/export-contract.md` tells consumers to
    trust exactly that cell for freshness."""

    def test_a_fresh_row_wins_over_a_carried_one_whatever_the_order(self) -> None:
        old = MetaRow(feed="pricebook.categories", last_run_at="yesterday", row_count=7)
        fresh = MetaRow(feed="pricebook.categories", last_run_at="today", row_count=3)

        carry_first = MetaRowSet()
        carry_first.carry(old)
        carry_first.add(fresh)

        add_first = MetaRowSet()
        add_first.add(fresh)
        add_first.carry(old)

        assert list(carry_first) == [fresh]
        assert list(add_first) == [fresh]

    def test_carrying_the_same_row_repeatedly_adds_one_row(self) -> None:
        rows = MetaRowSet()
        for _ in range(4):
            rows.carry(MetaRow(feed="jobs", last_run_at="yesterday"))
        assert len(rows) == 1

    def test_membership_is_by_tab_name(self) -> None:
        rows = MetaRowSet()
        rows.add(MetaRow(feed="jobs", last_run_at="today"))
        assert "jobs" in rows
        assert "technicians" not in rows

    def test_build_meta_grid_refuses_two_rows_for_one_tab(self) -> None:
        """The assertion at the boundary. `MetaRowSet` already makes this
        unrepresentable for a real run; this is what a future caller that
        hand-rolls a list gets instead of a stale timestamp."""
        rows = [
            MetaRow(feed="pricebook.services", last_run_at="today"),
            MetaRow(feed="pricebook.services", last_run_at="yesterday"),
        ]
        with pytest.raises(ValueError, match="pricebook.services"):
            build_meta_grid(rows)

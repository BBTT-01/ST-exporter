from __future__ import annotations

from st_exporter.feeds.raw_cache import RawCache


def test_merge_adds_new_records() -> None:
    cache = RawCache()
    cache.merge([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    assert cache.get(1) == {"id": 1, "name": "a"}
    assert cache.get(2) == {"id": 2, "name": "b"}


def test_merge_overwrites_by_id_last_write_wins() -> None:
    cache = RawCache()
    cache.merge([{"id": 1, "name": "old"}])
    cache.merge([{"id": 1, "name": "new"}])
    assert cache.get(1) == {"id": 1, "name": "new"}


def test_merge_leaves_untouched_ids_alone() -> None:
    cache = RawCache()
    cache.merge([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    cache.merge([{"id": 1, "name": "a-updated"}])
    assert cache.get(1) == {"id": 1, "name": "a-updated"}
    assert cache.get(2) == {"id": 2, "name": "b"}


def test_get_by_int_or_str_id_is_equivalent() -> None:
    cache = RawCache()
    cache.merge([{"id": 42, "name": "a"}])
    assert cache.get(42) == cache.get("42")


def test_to_grid_from_grid_round_trip() -> None:
    cache = RawCache()
    cache.merge([{"id": 2, "name": "b"}, {"id": 1, "name": "a", "nested": {"x": 1}}])
    grid = cache.to_grid()
    assert grid[0] == ["id", "payload_json"]
    # Sorted by id (string) for deterministic output.
    assert [row[0] for row in grid[1:]] == ["1", "2"]

    restored = RawCache.from_grid(grid)
    assert restored.get(1) == {"id": 1, "name": "a", "nested": {"x": 1}}
    assert restored.get(2) == {"id": 2, "name": "b"}


def test_from_grid_of_empty_grid_is_empty_cache() -> None:
    assert RawCache.from_grid([]).values() == []


def test_from_grid_skips_malformed_rows() -> None:
    grid = [["id", "payload_json"], ["1", "not json"], ["2", '{"id": 2}']]
    restored = RawCache.from_grid(grid)
    assert restored.get(1) is None
    assert restored.get(2) == {"id": 2}


def test_merge_ignores_records_without_id() -> None:
    cache = RawCache()
    cache.merge([{"name": "no id here"}])
    assert cache.values() == []

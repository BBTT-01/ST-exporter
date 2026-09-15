"""Fetch-layer tests: the one-id-per-request quirk and the merge that follows it."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from st_exporter.feeds.pricebook import fetch_pricebook_categories, fetch_pricebook_items


@pytest.fixture()
def mock_client():
    return MagicMock()


def _envelope(data, has_more=False):
    return {"data": data, "hasMore": has_more}


def test_unfiltered_fetch_is_a_single_pass_over_the_whole_catalogue(mock_client) -> None:
    mock_client.get.return_value = _envelope([{"id": 1}])
    assert fetch_pricebook_items(mock_client, "services") == [{"id": 1}]
    mock_client.get.assert_called_once_with(
        "pricebook", "services", params={"active": "Any", "page": 1, "pageSize": 200}
    )


def test_active_any_so_withdrawn_items_still_export(mock_client) -> None:
    # Without active=Any a deactivated item vanishes from the tab instead of
    # exporting as active=false, and consumers never delete — they mark withdrawn.
    mock_client.get.return_value = _envelope([])
    fetch_pricebook_items(mock_client, "materials")
    assert mock_client.get.call_args.kwargs["params"]["active"] == "Any"


def test_categories_are_fetched_one_id_per_request_never_batched(mock_client) -> None:
    mock_client.get.side_effect = [_envelope([{"id": 1}]), _envelope([{"id": 2}])]
    items = fetch_pricebook_items(mock_client, "equipment", category_ids=[10, 11])

    sent = [call.kwargs["params"]["categoryIds"] for call in mock_client.get.call_args_list]
    assert sent == ["10", "11"]
    assert {str(item["id"]) for item in items} == {"1", "2"}


def test_an_item_in_two_categories_is_merged_not_duplicated(mock_client) -> None:
    mock_client.get.side_effect = [
        _envelope([{"id": 1, "code": "A", "categories": [{"id": 10, "name": "Doors"}]}]),
        _envelope([{"id": 1, "code": "A", "categories": [{"id": 11, "name": "Steel"}]}]),
    ]
    items = fetch_pricebook_items(mock_client, "equipment", category_ids=[10, 11])

    assert len(items) == 1
    assert [c["id"] for c in items[0]["categories"]] == [10, 11]


def test_merge_unions_assets_across_serial_requests(mock_client) -> None:
    mock_client.get.side_effect = [
        _envelope([{"id": 1, "assets": [{"id": "a1", "url": "u1"}]}]),
        _envelope([{"id": 1, "assets": [{"id": "a1", "url": "u1"}, {"id": "a2", "url": "u2"}]}]),
    ]
    items = fetch_pricebook_items(mock_client, "services", category_ids=[10, 11])
    assert [a["id"] for a in items[0]["assets"]] == ["a1", "a2"]


def test_a_single_category_id_still_takes_the_plain_path(mock_client) -> None:
    mock_client.get.return_value = _envelope([{"id": 1}])
    fetch_pricebook_items(mock_client, "services", category_ids=["10"])
    assert mock_client.get.call_count == 1
    assert mock_client.get.call_args.kwargs["params"]["categoryIds"] == "10"


def test_blank_category_ids_are_ignored(mock_client) -> None:
    mock_client.get.return_value = _envelope([{"id": 1}])
    fetch_pricebook_items(mock_client, "services", category_ids=["", "  "])
    assert "categoryIds" not in mock_client.get.call_args.kwargs["params"]


def test_an_item_without_an_id_is_dropped_from_a_merged_fetch(mock_client) -> None:
    # It can't be keyed or deduped, and st_id must be non-empty anyway.
    mock_client.get.side_effect = [_envelope([{"code": "no-id"}]), _envelope([{"id": 2}])]
    items = fetch_pricebook_items(mock_client, "services", category_ids=[10, 11])
    assert [item["id"] for item in items] == [2]


def test_pagination_is_followed(mock_client) -> None:
    mock_client.get.side_effect = [_envelope([{"id": 1}], has_more=True), _envelope([{"id": 2}])]
    items = fetch_pricebook_items(mock_client, "services")
    assert [item["id"] for item in items] == [1, 2]
    assert [c.kwargs["params"]["page"] for c in mock_client.get.call_args_list] == [1, 2]


def test_categories_endpoint_is_unfiltered(mock_client) -> None:
    mock_client.get.return_value = _envelope([{"id": 10, "name": "Doors"}])
    assert fetch_pricebook_categories(mock_client) == [{"id": 10, "name": "Doors"}]
    mock_client.get.assert_called_once_with(
        "pricebook", "categories", params={"active": "Any", "page": 1, "pageSize": 200}
    )

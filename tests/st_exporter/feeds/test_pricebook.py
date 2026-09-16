"""Fetch-layer tests: the one-id-per-request quirk and the merge that follows it."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from st_cli.exceptions import APIError
from st_exporter.feeds.pricebook import (
    category_name_index,
    fetch_pricebook_categories,
    fetch_pricebook_items,
)
from st_exporter.pricebook import build_item_row


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


class TestTheTwoCategoryShapes:
    """`categories` is objects on `services` and bare int ids on `equipment`/`materials`.

    Source: `tenant-pricebook-v2`'s OpenAPI —
    ``Pricebook.V2.ServiceResponse.categories`` is an array of
    ``Pricebook.V2.SkuCategoryResponse`` (``id``/``name``/``active``), while
    ``Pricebook.V2.EquipmentResponse.categories`` and
    ``Pricebook.V2.MaterialResponse.categories`` are
    ``{"type": "array", "items": {"type": "integer", "format": "int64"}}``.

    That is exactly what run ``35134016237`` (tenant ``tr-doorservpro``, exporter
    0.2.11) showed: `pricebook.services` exported its two category columns while
    `pricebook.equipment` (10041 rows) and `pricebook.materials` (4990 rows) were
    blank in both — on a tenant whose same run exported 61 categories.

    The responses below are shaped like the real ones, not like the reader.
    """

    #: One page of `equipment` exactly as ServiceTitan sends it: bare int ids.
    LIVE_EQUIPMENT = {
        "id": 100,
        "code": "DOOR-16x7",
        "displayName": "16x7 Steel Door",
        "active": True,
        "price": 1299.5,
        "categories": [10, 11],
        "assets": [],
        "modifiedOn": "2026-09-03T00:00:00Z",
    }
    #: One page of `categories`, the endpoint the names come from.
    LIVE_CATEGORIES = [
        {"id": 10, "name": "Service", "active": True, "parentId": None},
        {"id": 11, "name": "Doors", "active": True, "parentId": 10},
    ]

    def test_bare_ids_are_named_from_the_categories_endpoint(self, mock_client) -> None:
        mock_client.get.side_effect = [
            _envelope([self.LIVE_EQUIPMENT]),
            _envelope(self.LIVE_CATEGORIES),
        ]
        items = fetch_pricebook_items(mock_client, "equipment")

        # BEFORE this fix the reader saw ints where it expected objects, skipped
        # every one, and produced ("", "") — the blank columns the live run found.
        assert build_item_row(items[0])["category_ids"] == "10,11"
        assert build_item_row(items[0])["category_names"] == "Service,Doors"

    def test_the_object_shape_costs_no_extra_request(self, mock_client) -> None:
        # `services` already carries names, so the categories endpoint is never hit.
        mock_client.get.side_effect = [
            _envelope([{"id": 1, "categories": [{"id": 10, "name": "Service"}]}]),
        ]
        items = fetch_pricebook_items(mock_client, "services")
        assert mock_client.get.call_count == 1
        assert build_item_row(items[0])["category_names"] == "Service"

    def test_an_unknown_id_keeps_its_slot_with_a_blank_name(self, mock_client) -> None:
        mock_client.get.side_effect = [
            _envelope([{**self.LIVE_EQUIPMENT, "categories": [10, 999]}]),
            _envelope(self.LIVE_CATEGORIES),
        ]
        row = build_item_row(fetch_pricebook_items(mock_client, "equipment")[0])
        assert row["category_ids"].split(",") == ["10", "999"]
        assert row["category_names"].split(",") == ["Service", ""]

    def test_unreadable_categories_still_export_the_ids(self, mock_client) -> None:
        # Losing the names is not worth losing the tab: category_ids is a usable
        # join key on its own, and the blank-column detector then reports
        # category_names truthfully rather than the exporter inventing one.
        mock_client.get.side_effect = [
            _envelope([self.LIVE_EQUIPMENT]),
            APIError(500, "boom"),
        ]
        row = build_item_row(fetch_pricebook_items(mock_client, "equipment")[0])
        assert row["category_ids"] == "10,11"
        assert row["category_names"] == ","

    def test_an_ungranted_categories_permission_is_not_a_warning(self, mock_client, caplog) -> None:
        # A 403 is "the contractor never bought Pricebook -> Categories", which
        # scopes.py keeps at INFO for the tab; warning once per item resource on
        # every run forever would be noise, not signal.
        mock_client.get.side_effect = [
            _envelope([self.LIVE_EQUIPMENT]),
            APIError(403, "Scope validation failed"),
        ]
        with caplog.at_level(logging.INFO, logger="st_exporter"):
            fetch_pricebook_items(mock_client, "equipment")
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_bare_ids_merge_across_the_serial_category_requests(self, mock_client) -> None:
        # Quirks 1+3 together: one item returned by two one-id requests, in the
        # nameless shape. The union must key on the id inside the normalised entry.
        mock_client.get.side_effect = [
            _envelope([{"id": 1, "categories": [10]}]),
            _envelope([{"id": 1, "categories": [11]}]),
            _envelope(self.LIVE_CATEGORIES),
        ]
        items = fetch_pricebook_items(mock_client, "equipment", category_ids=[10, 11])
        assert len(items) == 1
        assert build_item_row(items[0])["category_names"] == "Service,Doors"

    def test_the_name_index_walks_nested_subcategories(self) -> None:
        # Pricebook.V2.CategoryResponse nests `subcategories`, and an item may
        # reference a nested id, so a top-level-only index would blank those names.
        index = category_name_index(
            [{"id": 10, "name": "Service", "subcategories": [{"id": 11, "name": "Doors"}]}]
        )
        assert index == {"10": "Service", "11": "Doors"}

    def test_a_record_without_categories_is_untouched(self, mock_client) -> None:
        mock_client.get.return_value = _envelope([{"id": 1, "code": "A"}])
        assert fetch_pricebook_items(mock_client, "equipment") == [{"id": 1, "code": "A"}]

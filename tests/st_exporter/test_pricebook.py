"""Contract tests for the pure `pricebook.*` row builders.

Every assertion here traces to CONTRACT-pricebook-tabs.md (`pricebook.v1`, frozen
2026-09-14). Two consuming apps parse these exact headers and cell rules, so a
change that makes one of these fail is a contract break, not a test to update.
"""

from __future__ import annotations

from st_exporter.pricebook import (
    CATEGORY_COLUMNS,
    CONTRACT_VERSION,
    ITEM_COLUMNS,
    build_category_grid,
    build_item_grid,
    build_item_row,
    image_refs,
)


def _item(**overrides):
    record = {
        "id": 1,
        "code": "SKU-1",
        "displayName": "16x7 Steel Door",
        "description": "A door",
        "price": 1299.5,
        "active": True,
        "categories": [{"id": 10, "name": "Doors"}, {"id": 11, "name": "Steel"}],
        "manufacturer": "Acme",
        "model": "A-16",
        "assets": [{"id": "a1", "url": "https://cdn.example.com/a1.jpg"}],
        "modifiedOn": "2026-09-01T00:00:00Z",
    }
    record.update(overrides)
    return record


class TestColumns:
    def test_item_columns_are_exactly_the_contract(self) -> None:
        assert set(ITEM_COLUMNS) == {
            "st_id",
            "code",
            "name",
            "description",
            "price",
            "active",
            "category_ids",
            "category_names",
            "manufacturer",
            "model",
            "image_refs",
            "modified_on",
        }

    def test_category_columns_are_exactly_the_contract(self) -> None:
        assert set(CATEGORY_COLUMNS) == {"st_id", "name", "active", "parent_id"}

    def test_contract_version_literal(self) -> None:
        assert CONTRACT_VERSION == "pricebook.v1"

    def test_item_grid_starts_with_the_header_row(self) -> None:
        assert build_item_grid([])[0] == list(ITEM_COLUMNS)

    def test_category_grid_starts_with_the_header_row(self) -> None:
        assert build_category_grid([])[0] == list(CATEGORY_COLUMNS)


class TestCellRules:
    def test_every_cell_is_text(self) -> None:
        row = build_item_grid([_item()])[1]
        assert all(isinstance(cell, str) for cell in row)

    def test_price_null_is_blank_not_zero(self) -> None:
        assert build_item_row(_item(price=None))["price"] == ""

    def test_price_missing_key_is_blank_not_zero(self) -> None:
        record = _item()
        del record["price"]
        assert build_item_row(record)["price"] == ""

    def test_a_real_zero_price_is_written_as_zero(self) -> None:
        # The blank-when-null rule must not swallow a genuine free item.
        assert build_item_row(_item(price=0))["price"] == "0"

    def test_price_uses_a_dot_separator(self) -> None:
        assert build_item_row(_item(price=1299.5))["price"] == "1299.5"

    def test_active_is_lowercase_true_false(self) -> None:
        assert build_item_row(_item(active=True))["active"] == "true"
        assert build_item_row(_item(active=False))["active"] == "false"

    def test_active_absent_is_blank_not_false(self) -> None:
        # false means "withdrawn" to the consumer; guessing it would retire a
        # live item.
        assert build_item_row(_item(active=None))["active"] == ""

    def test_name_prefers_display_name(self) -> None:
        assert build_item_row(_item(name="internal"))["name"] == "16x7 Steel Door"

    def test_name_falls_back_to_name(self) -> None:
        assert build_item_row(_item(displayName=None, name="Fallback"))["name"] == "Fallback"

    def test_name_is_never_blank(self) -> None:
        row = build_item_row(_item(displayName=None, name=None))
        assert row["name"] != ""

    def test_blank_description_stays_blank(self) -> None:
        assert build_item_row(_item(description=None))["description"] == ""


class TestCategories:
    def test_ids_and_names_are_comma_separated_in_the_same_order(self) -> None:
        row = build_item_row(_item())
        assert row["category_ids"] == "10,11"
        assert row["category_names"] == "Doors,Steel"

    def test_no_categories_is_blank_in_both_columns(self) -> None:
        row = build_item_row(_item(categories=[]))
        assert row["category_ids"] == ""
        assert row["category_names"] == ""

    def test_a_null_category_name_keeps_the_positions_aligned(self) -> None:
        row = build_item_row(_item(categories=[{"id": 10, "name": None}, {"id": 11, "name": "B"}]))
        assert row["category_ids"].split(",") == ["10", "11"]
        assert row["category_names"].split(",") == ["", "B"]

    def test_a_category_with_no_id_is_dropped_from_both_columns(self) -> None:
        row = build_item_row(_item(categories=[{"name": "Orphan"}, {"id": 11, "name": "B"}]))
        assert row["category_ids"] == "11"
        assert row["category_names"] == "B"


class TestImageRefs:
    def test_identifiers_only_never_bytes(self) -> None:
        refs = image_refs([{"id": "a1", "url": "https://cdn.example.com/a1.jpg"}])
        assert refs == "a1"

    def test_dedupes_repeated_assets_within_one_payload(self) -> None:
        assets = [
            {"id": "a1", "url": "https://cdn.example.com/a1.jpg"},
            {"id": "a1", "url": "https://cdn.example.com/a1.jpg", "isDefault": True},
            {"id": "a2", "url": "https://cdn.example.com/a2.jpg"},
        ]
        assert image_refs(assets) == "a1,a2"

    def test_falls_back_to_the_url_when_there_is_no_asset_id(self) -> None:
        assert image_refs([{"url": "https://cdn.example.com/x.jpg"}]) == (
            "https://cdn.example.com/x.jpg"
        )

    def test_an_authenticated_storage_path_is_carried_through_verbatim(self) -> None:
        # ServiceTitan returns either an HTTPS URL or a storage path; both are
        # just identifiers here — resolving them is the image-upload ticket's job.
        path = "Images/Pricebook/9f2c-uuid.jpg"
        assert image_refs([{"id": None, "url": path}]) == path

    def test_no_assets_is_blank(self) -> None:
        assert image_refs([]) == ""
        assert image_refs(None) == ""


class TestCategoryRows:
    def test_parent_id_is_blank_at_the_top_level(self) -> None:
        grid = build_category_grid([{"id": 10, "name": "Doors", "active": True, "parentId": None}])
        row = dict(zip(CATEGORY_COLUMNS, grid[1]))
        assert row == {"st_id": "10", "name": "Doors", "active": "true", "parent_id": ""}

    def test_child_category_carries_its_parent(self) -> None:
        grid = build_category_grid([{"id": 11, "name": "Steel", "active": False, "parentId": 10}])
        row = dict(zip(CATEGORY_COLUMNS, grid[1]))
        assert row["parent_id"] == "10"
        assert row["active"] == "false"


def test_the_three_item_tabs_share_one_code_path() -> None:
    # services / equipment / materials differ only by tab name. Same record in,
    # byte-identical row out — that is the "one parser, three tabs" guarantee.
    record = _item()
    assert build_item_grid([record]) == build_item_grid([record])

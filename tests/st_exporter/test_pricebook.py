"""Contract tests for the pure `pricebook.*` row builders.

Every assertion here traces to CONTRACT-pricebook-tabs.md (`pricebook.v2`). Two
consuming apps parse these exact headers and cell rules, so a change that makes
one of these fail is a contract break, not a test to update.
"""

from __future__ import annotations

from st_exporter.pricebook import (
    CATEGORY_COLUMNS,
    CONTRACT_VERSION,
    ITEM_COLUMNS,
    build_category_grid,
    build_category_row,
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
        "cost": 640,
        "hours": 3,
        "active": True,
        "categories": [{"id": 10, "name": "Doors"}, {"id": 11, "name": "Steel"}],
        "manufacturer": "Acme",
        "model": "A-16",
        "assets": [{"id": "a1", "url": "https://cdn.example.com/a1.jpg"}],
        "modifiedOn": "2026-09-01T00:00:00Z",
    }
    record.update(overrides)
    return record


#: The `pricebook.v1` header, verbatim. It is spelled out here rather than sliced
#: off `ITEM_COLUMNS`, because a slice of the thing under test proves nothing: it
#: would follow a rename straight over the cliff.
_V1_ITEM_COLUMNS = (
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
)

_V1_CATEGORY_COLUMNS = ("st_id", "name", "active", "parent_id")


class TestColumns:
    def test_the_v1_columns_are_untouched_and_still_lead(self) -> None:
        # The whole basis on which v2 is additive: a consumer reading any of the
        # original twelve, by name OR by position, sees exactly what it saw.
        assert ITEM_COLUMNS[: len(_V1_ITEM_COLUMNS)] == _V1_ITEM_COLUMNS

    def test_the_v1_category_columns_are_untouched_and_still_lead(self) -> None:
        assert CATEGORY_COLUMNS[: len(_V1_CATEGORY_COLUMNS)] == _V1_CATEGORY_COLUMNS

    def test_item_columns_are_exactly_the_contract(self) -> None:
        assert ITEM_COLUMNS == _V1_ITEM_COLUMNS + (
            "cost",
            "hours",
            "member_price",
            "add_on_price",
            "add_on_member_price",
            "taxable",
            "is_labor",
            "is_inventory",
            "deduct_as_job_cost",
            "pays_commission",
            "commission_bonus",
            "unit_of_measure",
            "cross_sale_group",
            "account",
            "cost_of_sale_account",
            "asset_account",
            "warranty_duration",
            "warranty_description",
            "manufacturer_warranty_duration",
            "manufacturer_warranty_description",
            "service_provider_warranty_duration",
            "service_provider_warranty_description",
            "primary_vendor_id",
            "primary_vendor_name",
            "primary_vendor_part",
            "primary_vendor_cost",
            "other_vendor_ids",
            "other_vendor_names",
            "source",
            "external_id",
        )

    def test_no_column_is_declared_twice(self) -> None:
        assert len(set(ITEM_COLUMNS)) == len(ITEM_COLUMNS)
        assert len(set(CATEGORY_COLUMNS)) == len(CATEGORY_COLUMNS)

    def test_category_columns_are_exactly_the_contract(self) -> None:
        assert CATEGORY_COLUMNS == _V1_CATEGORY_COLUMNS + (
            "description",
            "image",
            "position",
            "category_type",
            "business_unit_ids",
            "sku_image_refs",
            "sku_video_refs",
            "source",
            "external_id",
        )

    def test_contract_version_literal(self) -> None:
        assert CONTRACT_VERSION == "pricebook.v2"

    def test_cost_and_hours_lead_the_appended_block(self) -> None:
        # Appended, never inserted: a consumer reading by position must not have
        # every column after `price` shift under it. These two are the reason
        # v2 exists, so they come first of the new ones.
        assert ITEM_COLUMNS[12:14] == ("cost", "hours")

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

    def test_cost_comes_from_the_service_titan_cost_field(self) -> None:
        assert build_item_row(_item(cost=640))["cost"] == "640"

    def test_hours_comes_from_the_service_titan_hours_field(self) -> None:
        assert build_item_row(_item(hours=2.5))["hours"] == "2.5"

    def test_cost_null_is_blank_not_zero(self) -> None:
        # The expensive one: a null cost read as zero prices the item at pure
        # margin, which is how an unusable catalogue looks usable.
        assert build_item_row(_item(cost=None))["cost"] == ""

    def test_hours_null_is_blank_not_zero(self) -> None:
        assert build_item_row(_item(hours=None))["hours"] == ""

    def test_a_missing_cost_key_is_blank_not_zero(self) -> None:
        # Every `services` record takes this path: ServiceTitan's
        # Pricebook.V2.ServiceResponse has no cost field at all.
        record = _item()
        del record["cost"]
        assert build_item_row(record)["cost"] == ""

    def test_a_missing_hours_key_is_blank_not_zero(self) -> None:
        record = _item()
        del record["hours"]
        assert build_item_row(record)["hours"] == ""

    def test_a_real_zero_cost_is_written_as_zero(self) -> None:
        assert build_item_row(_item(cost=0))["cost"] == "0"

    def test_a_real_zero_hours_is_written_as_zero(self) -> None:
        assert build_item_row(_item(hours=0))["hours"] == "0"

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

    def test_bare_ids_still_fill_category_ids(self) -> None:
        # The REAL `equipment`/`materials` shape: `categories` is an array of bare
        # int64 ids (tenant-pricebook-v2: Pricebook.V2.{Equipment,Material}Response).
        # Reading only the object form is what blanked BOTH columns on all 10041
        # equipment and 4990 material rows of run 35134016237 (tr-doorservpro).
        # `feeds.pricebook` resolves the names before records reach here; this
        # pins that the ids survive even when it could not.
        row = build_item_row(_item(categories=[10, 11]))
        assert row["category_ids"] == "10,11"
        assert row["category_names"] == ","

    def test_bare_ids_and_objects_mix_without_losing_alignment(self) -> None:
        row = build_item_row(_item(categories=[10, {"id": 11, "name": "Steel"}]))
        assert row["category_ids"].split(",") == ["10", "11"]
        assert row["category_names"].split(",") == ["", "Steel"]


class TestTheFullPayload:
    """v2 emits every scalar ServiceTitan returns. These pin HOW, not just THAT."""

    def test_a_nested_object_is_flattened_into_prefixed_columns(self) -> None:
        row = build_item_row(
            _item(primaryVendor={"vendorId": 77, "vendorName": "Acme Supply", "cost": 410.25})
        )
        assert row["primary_vendor_id"] == "77"
        assert row["primary_vendor_name"] == "Acme Supply"
        assert row["primary_vendor_cost"] == "410.25"

    def test_an_absent_nested_object_leaves_every_one_of_its_columns_blank(self) -> None:
        row = build_item_row(_item(primaryVendor=None))
        assert row["primary_vendor_id"] == ""
        assert row["primary_vendor_name"] == ""
        assert row["primary_vendor_cost"] == ""

    def test_a_null_inside_a_nested_object_is_blank_not_zero(self) -> None:
        row = build_item_row(_item(primaryVendor={"vendorId": 77, "cost": None}))
        assert row["primary_vendor_cost"] == ""

    def test_a_real_zero_inside_a_nested_object_is_written_as_zero(self) -> None:
        row = build_item_row(_item(primaryVendor={"vendorId": 77, "cost": 0}))
        assert row["primary_vendor_cost"] == "0"

    def test_a_list_of_objects_uses_the_category_convention(self) -> None:
        # Index-aligned id/name CSV, the SAME style as category_ids/category_names
        # rather than a second one invented beside it.
        row = build_item_row(
            _item(
                otherVendors=[
                    {"vendorId": 1, "vendorName": "One"},
                    {"vendorId": 2, "vendorName": "Two"},
                ]
            )
        )
        assert row["other_vendor_ids"] == "1,2"
        assert row["other_vendor_names"] == "One,Two"

    def test_a_vendor_with_no_id_is_dropped_from_both_columns(self) -> None:
        row = build_item_row(
            _item(otherVendors=[{"vendorName": "Orphan"}, {"vendorId": 2, "vendorName": "Two"}])
        )
        assert row["other_vendor_ids"] == "2"
        assert row["other_vendor_names"] == "Two"

    def test_the_two_equipment_warranties_never_share_a_column(self) -> None:
        # Folding them together would put a manufacturer's warranty and a service
        # provider's in one cell, wrong in a way no consumer could detect.
        row = build_item_row(
            _item(
                manufacturerWarranty={"duration": 120, "description": "10 year parts"},
                serviceProviderWarranty={"duration": 12, "description": "1 year labour"},
            )
        )
        assert row["manufacturer_warranty_duration"] == "120"
        assert row["manufacturer_warranty_description"] == "10 year parts"
        assert row["service_provider_warranty_duration"] == "12"
        assert row["service_provider_warranty_description"] == "1 year labour"
        assert row["warranty_duration"] == ""

    def test_the_service_warranty_has_its_own_columns(self) -> None:
        row = build_item_row(_item(warranty={"duration": 24, "description": "2 year"}))
        assert row["warranty_duration"] == "24"
        assert row["warranty_description"] == "2 year"
        assert row["manufacturer_warranty_duration"] == ""

    def test_the_other_money_columns_follow_the_same_blank_rule(self) -> None:
        assert build_item_row(_item(memberPrice=None))["member_price"] == ""
        assert build_item_row(_item(memberPrice=0))["member_price"] == "0"
        assert build_item_row(_item(commissionBonus=None))["commission_bonus"] == ""
        assert build_item_row(_item(commissionBonus=0))["commission_bonus"] == "0"

    def test_the_other_boolean_columns_follow_the_same_blank_rule(self) -> None:
        assert build_item_row(_item(taxable=True))["taxable"] == "true"
        assert build_item_row(_item(taxable=False))["taxable"] == "false"
        assert build_item_row(_item(taxable=None))["taxable"] == ""
        assert build_item_row(_item())["is_labor"] == ""

    def test_a_field_a_resource_does_not_have_is_simply_blank(self) -> None:
        # The union column set is what lets one parser read all three tabs.
        service = build_item_row(
            {"id": 1, "displayName": "Tune-Up", "price": 129, "hours": 1.5, "isLabor": True}
        )
        assert service["is_labor"] == "true"
        assert service["cost"] == ""
        assert service["unit_of_measure"] == ""

    def test_no_cell_is_ever_a_json_blob(self) -> None:
        # The failure this flattening exists to avoid: a dict or list stringified
        # into a cell, which reads as data and parses as nothing.
        row = build_item_row(
            _item(
                primaryVendor={"vendorId": 1, "vendorName": "One"},
                otherVendors=[{"vendorId": 2, "vendorName": "Two"}],
                warranty={"duration": 1, "description": "d"},
            )
        )
        for column, value in row.items():
            assert "{" not in value and "[" not in value, column

    def test_external_data_is_never_exported(self) -> None:
        # An arbitrary key/value bag any other integration can write to. It is the
        # one field here that could plausibly carry a token, and the Export Store
        # is not the place to find that out.
        row = build_item_row(
            _item(externalData=[{"key": "api_token", "value": "sk-live-do-not-publish"}])
        )
        assert "sk-live-do-not-publish" not in "".join(row.values())
        assert not any("external_data" in column for column in ITEM_COLUMNS)

    def test_a_bill_of_materials_is_never_flattened_to_bare_ids(self) -> None:
        # {skuId, quantity} per entry. A CSV of sku ids would look exactly like a
        # usable BOM while silently dropping every quantity.
        row = build_item_row(
            _item(equipmentMaterials=[{"skuId": 5, "quantity": 3}], recommendations=[{"skuId": 6}])
        )
        assert "5" not in row["other_vendor_ids"]
        assert not any(
            column.endswith(("_materials", "_equipment", "recommendations", "upgrades"))
            for column in ITEM_COLUMNS
        )


class TestCategoryFullPayload:
    def test_a_list_of_scalars_becomes_one_comma_separated_cell(self) -> None:
        row = build_category_row({"id": 10, "name": "Doors", "businessUnitIds": [1, 2, 3]})
        assert row["business_unit_ids"] == "1,2,3"

    def test_blank_entries_are_dropped_from_a_scalar_list(self) -> None:
        row = build_category_row({"id": 10, "name": "Doors", "skuImages": ["a.jpg", None, "b.jpg"]})
        assert row["sku_image_refs"] == "a.jpg,b.jpg"

    def test_the_recursive_subcategory_tree_is_not_exported(self) -> None:
        # parent_id already carries every edge in it, one row at a time.
        row = build_category_row(
            {"id": 10, "name": "Doors", "subcategories": [{"id": 11, "name": "Steel"}]}
        )
        assert all("subcategor" not in column for column in row)

    def test_position_is_blank_when_absent_and_zero_when_zero(self) -> None:
        assert build_category_row({"id": 10, "name": "D"})["position"] == ""
        assert build_category_row({"id": 10, "name": "D", "position": 0})["position"] == "0"


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
        assert row["parent_id"] == ""
        assert row["st_id"] == "10"
        assert row["name"] == "Doors"
        assert row["active"] == "true"
        # Everything the record did not carry is blank, never invented.
        assert all(row[column] == "" for column in CATEGORY_COLUMNS[4:])

    def test_child_category_carries_its_parent(self) -> None:
        grid = build_category_grid([{"id": 11, "name": "Steel", "active": False, "parentId": 10}])
        row = dict(zip(CATEGORY_COLUMNS, grid[1]))
        assert row["parent_id"] == "10"
        assert row["active"] == "false"


class TestTheKeyColumnIsNeverBlank:
    """``st_id`` is non-empty by contract, so a record with no id is DROPPED.

    Writing it instead produces a row whose key column is blank — which a
    consumer keyed on ``st_id`` discards anyway, while the run's ``row_count``
    still counted it, and which at a glance is indistinguishable from real data.
    The category-filtered fetch path already drops id-less records when it
    merges them; this is the same rule where every path meets.
    """

    def test_an_item_with_no_id_is_not_written(self) -> None:
        grid = build_item_grid([_item(), {**_item(), "id": None}])
        assert len(grid) == 2  # header + the one real item
        assert grid[1][0] == "1"

    def test_an_item_with_an_empty_string_id_is_not_written(self) -> None:
        assert build_item_grid([{**_item(), "id": ""}]) == [list(ITEM_COLUMNS)]

    def test_a_category_with_no_id_is_not_written(self) -> None:
        grid = build_category_grid([{"id": 10, "name": "Doors"}, {"name": "orphan"}])
        assert [row[0] for row in grid[1:]] == ["10"]

    def test_a_real_zero_id_is_still_a_real_id(self) -> None:
        # Guards the difference between "falsy" and "absent" — the same
        # distinction `price` turns on everywhere else in this module.
        assert build_item_grid([{**_item(), "id": 0}])[1][0] == "0"


def test_every_item_tab_is_built_by_the_same_parser() -> None:
    """ "One parser, three tabs": the tab name carries the meaning, the code does not.

    ``run.py`` must build services, equipment and materials through
    ``build_item_grid`` — if one of them ever grows its own path, this is what
    notices. Asserted on the wiring, not on ``f(x) == f(x)``.
    """
    from st_exporter import run

    assert set(run.PRICEBOOK_TABS) == {
        "pricebook.services",
        "pricebook.equipment",
        "pricebook.materials",
    }
    assert run.build_item_grid is build_item_grid
    assert run.build_category_grid is build_category_grid

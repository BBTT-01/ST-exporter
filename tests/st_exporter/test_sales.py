"""The pure record -> row mapping for the `sales.estimates` tab.

Every assertion here runs without a client, a Sheet or a network — the same
property ``test_financial.py`` and ``test_pricebook.py`` rely on to make the
grid recordable as a contract fixture.
"""

from __future__ import annotations

from st_exporter.sales import (
    CONTRACT_VERSION,
    ESTIMATE_COLUMNS,
    build_estimate_grid,
    build_estimate_rows,
)


def _rows(grid):
    return [dict(zip(grid[0], row)) for row in grid[1:]]


class TestContract:
    def test_contract_version_string(self) -> None:
        assert CONTRACT_VERSION == "sales.v1"

    def test_the_columns_the_spec_names_are_all_present_in_order(self) -> None:
        assert ESTIMATE_COLUMNS == (
            "EstimateId",
            "JobId",
            "JobNumber",
            "EstimateName",
            "Status",
            "Active",
            "SoldOn",
            "SoldById",
            "Subtotal",
            "Total",
            "ItemId",
            "ItemSkuId",
            "ItemSkuType",
            "ItemSkuSoldHours",
            "ItemQuantity",
            "ItemTotal",
            "ItemUnitCost",
            "ItemTotalCost",
            "ModifiedOn",
        )

    def test_empty_grid_still_leads_with_its_header(self) -> None:
        assert build_estimate_grid([]) == [list(ESTIMATE_COLUMNS)]

    def test_every_cell_is_text(self) -> None:
        grid = build_estimate_grid([{"id": 1, "jobId": 2, "items": [{"id": 10, "total": 9.5}]}])
        assert all(isinstance(cell, str) for row in grid for cell in row)


class TestOneRowPerItem:
    def test_two_items_produce_two_rows_sharing_the_estimate_columns(self) -> None:
        rows = _rows(
            build_estimate_grid(
                [
                    {
                        "id": 700,
                        "job": {"id": 7, "jobNumber": "J-7"},
                        "name": "Door replacement",
                        "status": {"name": "Sold"},
                        "active": True,
                        "soldOn": "2026-09-01T00:00:00Z",
                        "soldBy": {"id": 901},
                        "subtotal": 1000,
                        "total": 900,
                        "modifiedOn": "2026-09-01T00:00:00Z",
                        "items": [
                            {
                                "id": 7001,
                                "sku": {"id": 55, "type": "Service", "soldHours": 2.5},
                                "qty": 1,
                                "total": 300,
                                "unitCost": 40,
                                "totalCost": 40,
                            },
                            {
                                "id": 7002,
                                "sku": {"id": 56, "type": "Material"},
                                "qty": 4,
                                "total": 700,
                                "unitCost": None,
                                "totalCost": None,
                            },
                        ],
                    }
                ]
            )
        )
        assert len(rows) == 2
        assert {row["EstimateId"] for row in rows} == {"700"}
        assert rows[0]["JobId"] == "7"
        assert rows[0]["JobNumber"] == "J-7"
        assert rows[0]["Status"] == "Sold"
        assert rows[0]["Active"] == "true"
        assert rows[0]["SoldOn"] == "2026-09-01T00:00:00Z"
        assert rows[0]["SoldById"] == "901"
        assert rows[0]["Subtotal"] == "1000"
        assert rows[0]["Total"] == "900"
        assert rows[0]["ItemId"] == "7001"
        assert rows[0]["ItemSkuId"] == "55"
        assert rows[0]["ItemSkuType"] == "Service"
        assert rows[0]["ItemSkuSoldHours"] == "2.5"
        assert rows[0]["ItemUnitCost"] == "40"
        assert rows[0]["ItemTotalCost"] == "40"
        # Absent cost/hours are blank, never "0" — the second item's cost fields.
        assert rows[1]["ItemUnitCost"] == ""
        assert rows[1]["ItemTotalCost"] == ""

    def test_an_estimate_with_no_items_writes_one_row_with_item_columns_blank(self) -> None:
        rows = _rows(
            build_estimate_grid(
                [{"id": 701, "jobId": 8, "name": "Follow-up", "status": "Open", "items": []}]
            )
        )
        assert len(rows) == 1
        assert rows[0]["EstimateId"] == "701"
        assert rows[0]["JobId"] == "8"
        assert rows[0]["Status"] == "Open"
        for column in (
            "ItemId",
            "ItemSkuId",
            "ItemSkuType",
            "ItemSkuSoldHours",
            "ItemQuantity",
            "ItemTotal",
            "ItemUnitCost",
            "ItemTotalCost",
        ):
            assert rows[0][column] == ""

    def test_an_estimate_with_no_id_is_dropped(self) -> None:
        rows = build_estimate_rows([{"jobId": 9, "name": "Dropped", "items": []}])
        assert rows == []


class TestNeverSold:
    def test_sold_on_and_sold_by_id_are_blank_when_never_sold(self) -> None:
        rows = _rows(build_estimate_grid([{"id": 1, "status": "Open", "items": []}]))
        assert rows[0]["SoldOn"] == ""
        assert rows[0]["SoldById"] == ""


class TestStatus:
    def test_status_object_prefers_name_over_value(self) -> None:
        rows = _rows(
            build_estimate_grid([{"id": 1, "status": {"value": 2, "name": "Sold"}, "items": []}])
        )
        assert rows[0]["Status"] == "Sold"

    def test_status_object_falls_back_to_value_when_no_name(self) -> None:
        rows = _rows(build_estimate_grid([{"id": 1, "status": {"value": 2}, "items": []}]))
        assert rows[0]["Status"] == "2"

    def test_a_bare_scalar_status_passes_through(self) -> None:
        rows = _rows(build_estimate_grid([{"id": 1, "status": "Open", "items": []}]))
        assert rows[0]["Status"] == "Open"


class TestActiveBoolean:
    def test_active_true_and_false_are_lowercase(self) -> None:
        rows = _rows(
            build_estimate_grid(
                [
                    {"id": 1, "active": True, "items": []},
                    {"id": 2, "active": False, "items": []},
                ]
            )
        )
        assert rows[0]["Active"] == "true"
        assert rows[1]["Active"] == "false"

    def test_active_is_blank_not_false_when_absent(self) -> None:
        rows = _rows(build_estimate_grid([{"id": 1, "items": []}]))
        assert rows[0]["Active"] == ""


class TestMoneyAndHours:
    def test_a_zero_item_total_is_not_blank(self) -> None:
        rows = _rows(
            build_estimate_grid(
                [{"id": 1, "items": [{"id": 10, "total": 0, "unitCost": 0, "totalCost": 0}]}]
            )
        )
        assert rows[0]["ItemTotal"] == "0"
        assert rows[0]["ItemUnitCost"] == "0"
        assert rows[0]["ItemTotalCost"] == "0"

    def test_unknown_sold_hours_is_blank_not_zero(self) -> None:
        rows = _rows(build_estimate_grid([{"id": 1, "items": [{"id": 10}]}]))
        assert rows[0]["ItemSkuSoldHours"] == ""

    def test_subtotal_and_total_are_blank_when_absent(self) -> None:
        rows = _rows(build_estimate_grid([{"id": 1, "items": []}]))
        assert rows[0]["Subtotal"] == ""
        assert rows[0]["Total"] == ""


class TestJobAndSoldByReferences:
    def test_job_id_and_number_read_the_nested_job_object(self) -> None:
        rows = _rows(
            build_estimate_grid([{"id": 1, "job": {"id": 7, "jobNumber": "J-7"}, "items": []}])
        )
        assert rows[0]["JobId"] == "7"
        assert rows[0]["JobNumber"] == "J-7"

    def test_job_id_and_number_fall_back_to_flat_fields(self) -> None:
        rows = _rows(build_estimate_grid([{"id": 1, "jobId": 7, "jobNumber": "J-7", "items": []}]))
        assert rows[0]["JobId"] == "7"
        assert rows[0]["JobNumber"] == "J-7"

    def test_sold_by_id_reads_the_nested_object_or_the_flat_field(self) -> None:
        nested = _rows(build_estimate_grid([{"id": 1, "soldBy": {"id": 9}, "items": []}]))
        flat = _rows(build_estimate_grid([{"id": 2, "soldById": 9, "items": []}]))
        scalar = _rows(build_estimate_grid([{"id": 3, "soldBy": 9, "items": []}]))
        assert nested[0]["SoldById"] == "9"
        assert flat[0]["SoldById"] == "9"
        assert scalar[0]["SoldById"] == "9", (
            "the Estimates API returns soldBy as the bare employee id — the shape "
            "this repo's own `estimates-sell` examples send"
        )

    def test_total_falls_back_to_subtotal_plus_tax_only_when_both_are_present(self) -> None:
        """The response carries `subtotal` and `tax` separately and (as far as is
        known) no `total`; the sum is taken only when both parts are real numbers.
        A missing tax is unknown, not 0, so the cell stays blank — never `0`."""
        rows = _rows(
            build_estimate_grid(
                [
                    {"id": 1, "total": 900, "subtotal": 1000, "tax": 80, "items": []},
                    {"id": 2, "subtotal": 1000, "tax": 80, "items": []},
                    {"id": 3, "subtotal": 1000, "items": []},
                    {"id": 4, "subtotal": 1000, "tax": None, "items": []},
                    {"id": 5, "subtotal": 0, "tax": 0, "items": []},
                ]
            )
        )
        by_id = {row["EstimateId"]: row["Total"] for row in rows}
        assert by_id == {"1": "900", "2": "1080", "3": "", "4": "", "5": "0"}


class TestItemSkuFallback:
    def test_sku_fields_fall_back_to_flat_item_fields(self) -> None:
        rows = _rows(
            build_estimate_grid(
                [
                    {
                        "id": 1,
                        "items": [
                            {
                                "id": 10,
                                "skuId": 99,
                                "skuType": "Equipment",
                                "skuSoldHours": 3,
                                "quantity": 2,
                            }
                        ],
                    }
                ]
            )
        )
        assert rows[0]["ItemSkuId"] == "99"
        assert rows[0]["ItemSkuType"] == "Equipment"
        assert rows[0]["ItemSkuSoldHours"] == "3"
        assert rows[0]["ItemQuantity"] == "2"

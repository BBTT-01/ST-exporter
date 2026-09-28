"""The pure record -> row mapping for the three pricebook bill-of-materials tabs.

Every assertion here runs without a client, a Sheet or a network — the same
property ``test_pricebook.py`` and ``test_sales.py`` rely on to make the grid
recordable as a contract fixture.
"""

from __future__ import annotations

from st_exporter import contracts, run
from st_exporter.pricebook_bom import (
    CONTRACT_VERSION,
    EQUIPMENT_MATERIALS_TAB,
    LINK_COLUMNS,
    LINK_TABS,
    SERVICE_EQUIPMENT_TAB,
    SERVICE_MATERIALS_TAB,
    build_equipment_materials_grid,
    build_link_grid,
    build_service_equipment_grid,
    build_service_materials_grid,
)


def _data(grid: list[list[str]]) -> list[list[str]]:
    assert grid[0] == list(LINK_COLUMNS)
    return grid[1:]


class TestContract:
    def test_contract_version_string(self) -> None:
        assert CONTRACT_VERSION == "pricebook_bom.v1"

    def test_the_columns(self) -> None:
        assert LINK_COLUMNS == ("parent_st_id", "sku_id", "quantity")

    def test_each_tab_names_its_parent_resource_and_servicetitan_field(self) -> None:
        assert LINK_TABS == {
            "pricebook.serviceMaterials": ("services", "serviceMaterials"),
            "pricebook.serviceEquipment": ("services", "serviceEquipment"),
            "pricebook.equipmentMaterials": ("equipment", "equipmentMaterials"),
        }

    def test_the_tabs_are_published_under_their_own_version_not_pricebook_v2(self) -> None:
        for tab in (SERVICE_MATERIALS_TAB, SERVICE_EQUIPMENT_TAB, EQUIPMENT_MATERIALS_TAB):
            feed, contract = contracts.tabs()[tab]
            assert feed.version == "pricebook_bom.v1"
            assert contract.columns == LINK_COLUMNS
            assert contract.row_key == ()

    def test_the_tabs_carry_no_permission_of_their_own(self) -> None:
        """Derived from the parent payloads: a 403 belongs to `pricebook.services`
        or `pricebook.equipment`, never to a link tab."""
        assert not set(LINK_TABS) & set(run.EXPORT_TABS)
        assert set(run.PRICEBOOK_LINK_TAB_NAMES) == set(LINK_TABS)


class TestRows:
    def test_one_row_per_entry_in_servicetitan_order(self) -> None:
        records = [
            {
                "id": 1,
                "serviceMaterials": [{"skuId": 20, "quantity": 2}, {"skuId": 21, "quantity": 1}],
            },
            {"id": 2, "serviceMaterials": [{"skuId": 20, "quantity": 5}]},
        ]
        assert _data(build_link_grid(records, "serviceMaterials")) == [
            ["1", "20", "2"],
            ["1", "21", "1"],
            ["2", "20", "5"],
        ]

    def test_a_null_quantity_is_blank_never_zero(self) -> None:
        records = [{"id": 1, "serviceMaterials": [{"skuId": 20, "quantity": None}]}]
        assert _data(build_link_grid(records, "serviceMaterials")) == [["1", "20", ""]]

    def test_a_real_zero_and_a_fraction_are_written_as_they_are(self) -> None:
        records = [
            {
                "id": 1,
                "serviceMaterials": [{"skuId": 20, "quantity": 0}, {"skuId": 21, "quantity": 1.5}],
            }
        ]
        assert _data(build_link_grid(records, "serviceMaterials")) == [
            ["1", "20", "0"],
            ["1", "21", "1.5"],
        ]

    def test_an_entry_with_no_sku_is_dropped(self) -> None:
        records = [
            {
                "id": 1,
                "serviceMaterials": [
                    {"quantity": 3},
                    {"skuId": None, "quantity": 1},
                    {"skuId": "  ", "quantity": 1},
                    {"skuId": 20, "quantity": 1},
                ],
            }
        ]
        assert _data(build_link_grid(records, "serviceMaterials")) == [["1", "20", "1"]]

    def test_a_parent_with_no_id_contributes_nothing(self) -> None:
        records = [{"id": None, "serviceMaterials": [{"skuId": 20, "quantity": 1}]}, {"code": "X"}]
        assert _data(build_link_grid(records, "serviceMaterials")) == []

    def test_malformed_lists_and_entries_are_skipped_not_raised(self) -> None:
        records = [
            {"id": 1, "serviceMaterials": None},
            {"id": 2, "serviceMaterials": "20"},
            {"id": 3, "serviceMaterials": [20, ["21"], {"skuId": 22, "quantity": 1}]},
            {"id": 4},
        ]
        assert _data(build_link_grid(records, "serviceMaterials")) == [["3", "22", "1"]]

    def test_a_withdrawn_parent_keeps_its_links(self) -> None:
        """The consumer joins to the item tab, which already carries `active`."""
        records = [{"id": 1, "active": False, "serviceMaterials": [{"skuId": 20, "quantity": 1}]}]
        assert _data(build_link_grid(records, "serviceMaterials")) == [["1", "20", "1"]]

    def test_a_repeated_sku_is_two_rows_not_a_sum(self) -> None:
        records = [
            {
                "id": 1,
                "serviceMaterials": [{"skuId": 20, "quantity": 1}, {"skuId": 20, "quantity": 2}],
            }
        ]
        assert _data(build_link_grid(records, "serviceMaterials")) == [
            ["1", "20", "1"],
            ["1", "20", "2"],
        ]

    def test_no_records_is_a_header_only_grid(self) -> None:
        assert build_link_grid([], "serviceMaterials") == [list(LINK_COLUMNS)]


class TestEachTabReadsOnlyItsOwnList:
    RECORD = {
        "id": 7,
        "serviceMaterials": [{"skuId": 20, "quantity": 1}],
        "serviceEquipment": [{"skuId": 30, "quantity": 2}],
        "equipmentMaterials": [{"skuId": 40, "quantity": 3}],
    }

    def test_service_materials(self) -> None:
        assert _data(build_service_materials_grid([self.RECORD])) == [["7", "20", "1"]]

    def test_service_equipment(self) -> None:
        assert _data(build_service_equipment_grid([self.RECORD])) == [["7", "30", "2"]]

    def test_equipment_materials(self) -> None:
        assert _data(build_equipment_materials_grid([self.RECORD])) == [["7", "40", "3"]]

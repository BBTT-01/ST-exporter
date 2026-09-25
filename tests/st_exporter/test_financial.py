"""The pure record -> row mapping for the four `financial` tabs.

Every assertion here runs without a client, a Sheet or a network, which is the
property ticket 04 needs in order to record the grids as fixtures.

The header assertions are not tidiness: Profit Wizard's reader indexes these tabs
by column NAME (`lib/hosted/tabs.ts`), so a renamed column produces a tab that
parses to zero rows with only a warning. These tests are what stops that being a
silent change.
"""

from __future__ import annotations

import json
from pathlib import Path

from st_exporter.contracts import appended_columns
from st_exporter.financial import (
    BUSINESS_UNIT_COLUMNS,
    CONTRACT_VERSION,
    INVOICE_COLUMNS,
    JOB_COST_COLUMNS,
    TIMESHEET_COLUMNS,
    build_business_unit_grid,
    build_invoice_grid,
    build_job_cost_grid,
    build_timesheet_grid,
)


def _rows(grid):
    return [dict(zip(grid[0], row)) for row in grid[1:]]


class TestContract:
    def test_contract_version_string(self) -> None:
        assert CONTRACT_VERSION == "financial.v1"

    def test_the_columns_profit_wizard_reads_are_all_present(self) -> None:
        # Read straight off lib/hosted/tabs.ts's parsers. Losing any one of these
        # makes the corresponding tab parse to zero rows on their side.
        assert set(INVOICE_COLUMNS) >= {
            "JobId",
            "ReferenceNumber",
            "ItemType",
            "ItemTotal",
            "ItemCost",
            "ItemTotalCost",
            "ItemQuantity",
        }
        assert set(TIMESHEET_COLUMNS) >= {
            "JobId",
            "TechnicianId",
            "ArrivedOn",
            "DoneOn",
            "CanceledOn",
        }
        assert set(BUSINESS_UNIT_COLUMNS) >= {"BusinessUnitId", "Name", "Address", "Active"}
        assert set(JOB_COST_COLUMNS) >= {
            "JobNumber",
            "MaterialEquipmentPurchaseOrderCosts",
            "MaterialTotals",
            "EquipmentCosts",
            "TotalCosts",
        }

    def test_every_grid_leads_with_its_header_even_when_empty(self) -> None:
        assert build_invoice_grid([]) == [list(INVOICE_COLUMNS)]
        assert build_timesheet_grid([]) == [list(TIMESHEET_COLUMNS)]
        assert build_business_unit_grid([]) == [list(BUSINESS_UNIT_COLUMNS)]
        assert build_job_cost_grid([]) == [list(JOB_COST_COLUMNS)]

    def test_every_cell_is_text(self) -> None:
        grid = build_invoice_grid(
            [{"id": 1, "jobId": 2, "items": [{"type": "Material", "quantity": 3, "total": 9.5}]}]
        )
        assert all(isinstance(cell, str) for row in grid for cell in row)


class TestInvoices:
    def test_one_row_per_line_item_not_per_invoice(self) -> None:
        grid = build_invoice_grid(
            [
                {
                    "id": 500,
                    "job": {"id": 7},
                    "referenceNumber": "J-7",
                    "items": [
                        {"type": "Material", "total": 100, "cost": 10, "totalCost": 20},
                        {"type": "Service", "total": 300, "cost": 0, "totalCost": 0},
                    ],
                }
            ]
        )
        rows = _rows(grid)
        assert len(rows) == 2
        assert [r["ItemType"] for r in rows] == ["Material", "Service"]
        assert all(r["JobId"] == "7" and r["ReferenceNumber"] == "J-7" for r in rows)

    def test_an_invoice_with_no_items_contributes_no_rows(self) -> None:
        # Nothing to cost. One row of blanks would read to the consumer as a zero.
        assert build_invoice_grid([{"id": 1, "jobId": 2, "items": []}]) == [list(INVOICE_COLUMNS)]

    def test_an_invoice_attributable_to_no_job_is_dropped(self) -> None:
        grid = build_invoice_grid([{"id": 1, "items": [{"type": "Material", "total": 5}]}])
        assert grid == [list(INVOICE_COLUMNS)]

    def test_reference_number_alone_is_enough_to_keep_a_line(self) -> None:
        # Profit Wizard keys on JobId OR ReferenceNumber (`num:<ref>`).
        grid = build_invoice_grid(
            [{"id": 1, "referenceNumber": "J-9", "items": [{"type": "Material", "total": 5}]}]
        )
        assert _rows(grid)[0]["ReferenceNumber"] == "J-9"

    def test_zero_cost_stays_zero_and_absent_cost_stays_blank(self) -> None:
        # Load-bearing: Profit Wizard's toNum reads "" as null and "0" as 0, and
        # its material maths branches on exactly that difference.
        grid = build_invoice_grid(
            [
                {
                    "id": 1,
                    "jobId": 2,
                    "items": [
                        {"type": "Material", "cost": 0, "totalCost": None, "total": 0},
                    ],
                }
            ]
        )
        row = _rows(grid)[0]
        assert row["ItemCost"] == "0"
        assert row["ItemTotalCost"] == ""
        assert row["ItemTotal"] == "0"

    def test_per_unit_cost_and_extended_cost_are_not_swapped(self) -> None:
        # Swapping them understates cost by the quantity factor on every
        # multi-unit line.
        grid = build_invoice_grid(
            [
                {
                    "id": 1,
                    "jobId": 2,
                    "items": [{"type": "Material", "cost": 10, "totalCost": 40, "quantity": 4}],
                }
            ]
        )
        row = _rows(grid)[0]
        assert (row["ItemCost"], row["ItemTotalCost"], row["ItemQuantity"]) == ("10", "40", "4")

    def test_nested_job_wins_over_the_flat_job_id(self) -> None:
        grid = build_invoice_grid(
            [{"id": 1, "job": {"id": 7}, "jobId": 999, "items": [{"type": "Service"}]}]
        )
        assert _rows(grid)[0]["JobId"] == "7"

    def test_flat_job_id_is_accepted_when_there_is_no_nested_job(self) -> None:
        grid = build_invoice_grid([{"id": 1, "jobId": 99, "items": [{"type": "Service"}]}])
        assert _rows(grid)[0]["JobId"] == "99"

    def test_sku_comes_from_the_nested_object_or_the_flat_field(self) -> None:
        grid = build_invoice_grid(
            [
                {
                    "id": 1,
                    "jobId": 2,
                    "items": [
                        {"type": "Material", "sku": {"id": 55, "name": "Hinge"}},
                        {"type": "Material", "skuId": 66, "skuName": "Track"},
                    ],
                }
            ]
        )
        rows = _rows(grid)
        assert [(r["SkuId"], r["SkuName"]) for r in rows] == [("55", "Hinge"), ("66", "Track")]

    def test_a_non_dict_item_is_skipped_not_crashed_on(self) -> None:
        grid = build_invoice_grid(
            [{"id": 1, "jobId": 2, "items": [None, "junk", {"type": "Service"}]}]
        )
        assert len(_rows(grid)) == 1


class TestTimesheets:
    def test_maps_the_dispatch_shaped_fields(self) -> None:
        grid = build_timesheet_grid(
            [
                {
                    "id": 11,
                    "jobId": 7,
                    "appointmentId": 8,
                    "technicianId": 9,
                    "dispatchedOn": "2026-09-01T08:00:00Z",
                    "arrivedOn": "2026-09-01T09:00:00Z",
                    "doneOn": "2026-09-01T11:30:00Z",
                    "canceledOn": None,
                }
            ]
        )
        row = _rows(grid)[0]
        assert row["JobId"] == "7"
        assert row["TechnicianId"] == "9"
        assert row["ArrivedOn"] == "2026-09-01T09:00:00Z"
        assert row["DoneOn"] == "2026-09-01T11:30:00Z"
        assert row["CanceledOn"] == ""

    def test_a_segment_with_no_job_id_is_dropped(self) -> None:
        # The consumer's key column; it would discard the row anyway, and keeping
        # it would make row_count disagree with what lands.
        assert build_timesheet_grid([{"id": 1, "technicianId": 2}]) == [list(TIMESHEET_COLUMNS)]

    def test_no_derived_hours_column(self) -> None:
        # Profit Wizard computes hours from ArrivedOn/DoneOn itself; duplicating
        # that arithmetic here would bake this exporter's rounding into its numbers.
        assert not [c for c in TIMESHEET_COLUMNS if "our" in c.lower()]

    def test_a_cancelled_segment_is_still_exported(self) -> None:
        # Exporting it and letting the consumer skip it is the honest split —
        # dropping it here would hide a cancellation the consumer may want to see.
        grid = build_timesheet_grid([{"id": 1, "jobId": 7, "canceledOn": "2026-09-01T10:00:00Z"}])
        assert _rows(grid)[0]["CanceledOn"] == "2026-09-01T10:00:00Z"


class TestBusinessUnits:
    def test_address_is_the_same_single_line_join_the_jobs_tab_uses(self) -> None:
        grid = build_business_unit_grid(
            [
                {
                    "id": 3,
                    "name": "HVAC North",
                    "active": True,
                    "address": {"street": "1 Main St", "city": "Denver", "state": "CO"},
                }
            ]
        )
        row = _rows(grid)[0]
        assert row["Address"] == "1 Main St, Denver, CO"
        assert row["Active"] == "true"

    def test_a_retired_unit_exports_as_false_rather_than_vanishing(self) -> None:
        grid = build_business_unit_grid([{"id": 3, "name": "Old", "active": False}])
        assert _rows(grid)[0]["Active"] == "false"

    def test_absent_active_is_blank_not_false(self) -> None:
        # false means "retired" to the consumer; guessing it hides a live unit.
        grid = build_business_unit_grid([{"id": 3, "name": "Unknown"}])
        assert _rows(grid)[0]["Active"] == ""

    def test_a_unit_with_no_id_is_dropped(self) -> None:
        assert build_business_unit_grid([{"name": "Nameless"}]) == [list(BUSINESS_UNIT_COLUMNS)]


class TestJobCosts:
    def test_report_rows_are_narrowed_to_the_frozen_columns(self) -> None:
        grid = build_job_cost_grid(
            [
                {
                    "JobNumber": "J-1",
                    "TotalCosts": 500,
                    "TotalRevenue": 900,
                    "MaterialTotals": 100,
                    "EquipmentCosts": 50,
                    "MaterialEquipmentPurchaseOrderCosts": 0,
                    "SomeOtherColumn": "ignored",
                }
            ]
        )
        assert grid[0] == list(JOB_COST_COLUMNS)
        assert "SomeOtherColumn" not in grid[0]
        row = _rows(grid)[0]
        assert row["JobNumber"] == "J-1"
        assert row["TotalCosts"] == "500"

    def test_a_row_without_a_job_number_is_dropped(self) -> None:
        # mapCostingRow returns null for these; counting them would inflate _meta.
        grid = build_job_cost_grid([{"JobNumber": "", "TotalCosts": 1}, {"TotalCosts": 2}])
        assert grid == [list(JOB_COST_COLUMNS)]

    def test_a_zero_mepo_is_zero_and_a_missing_one_is_blank(self) -> None:
        # Profit Wizard's mapCostingRow treats MEPO==0 and MEPO==null differently:
        # 0 falls through to MaterialTotals+EquipmentCosts, null does not.
        grid = build_job_cost_grid(
            [
                {"JobNumber": "A", "MaterialEquipmentPurchaseOrderCosts": 0},
                {"JobNumber": "B", "MaterialEquipmentPurchaseOrderCosts": None},
                {"JobNumber": "C"},
            ]
        )
        values = [r["MaterialEquipmentPurchaseOrderCosts"] for r in _rows(grid)]
        assert values == ["0", "", ""]

    def test_column_order_is_frozen_so_two_runs_are_byte_identical(self) -> None:
        first = build_job_cost_grid([{"JobNumber": "A", "TotalCosts": 1}])
        second = build_job_cost_grid([{"TotalCosts": 1, "JobNumber": "A"}])
        assert first == second


_INVOICE_COLUMNS_BEFORE_TOTALS = (
    "JobId",
    "ReferenceNumber",
    "ItemType",
    "ItemTotal",
    "ItemCost",
    "ItemTotalCost",
    "ItemQuantity",
    "InvoiceId",
    "InvoiceDate",
    "SkuId",
    "SkuName",
    "BusinessUnitId",
)

_TOTALS = ("InvoiceSubTotal", "InvoiceSalesTax", "InvoiceTotal")


class TestInvoiceTotalsColumns:
    def test_the_three_totals_are_the_last_columns_in_order(self) -> None:
        assert INVOICE_COLUMNS[-3:] == _TOTALS
        assert all(INVOICE_COLUMNS.count(column) == 1 for column in _TOTALS)

    def test_every_earlier_column_keeps_its_name_and_position(self) -> None:
        assert INVOICE_COLUMNS[:-3] == _INVOICE_COLUMNS_BEFORE_TOTALS

    def test_the_append_is_pure_so_the_contract_version_stays_financial_v1(self) -> None:
        assert CONTRACT_VERSION == "financial.v1"
        assert appended_columns(_INVOICE_COLUMNS_BEFORE_TOTALS, INVOICE_COLUMNS) == list(_TOTALS)

    def test_the_released_fixture_stays_frozen_without_the_totals(self) -> None:
        fixture = (
            Path(__file__).resolve().parents[2]
            / "contracts/fixtures/financial.v1/accounting.invoices.json"
        )
        assert tuple(json.loads(fixture.read_text())["columns"]) == _INVOICE_COLUMNS_BEFORE_TOTALS

    def test_totals_come_from_the_invoice_record_and_repeat_on_every_line(self) -> None:
        grid = build_invoice_grid(
            [
                {
                    "id": 500,
                    "jobId": 7,
                    "subTotal": "400.00",
                    "salesTax": "33.00",
                    "total": "433.00",
                    "items": [
                        {"type": "Material", "total": 100},
                        {"type": "Service", "total": 300},
                    ],
                }
            ]
        )
        assert [tuple(r[c] for c in _TOTALS) for r in _rows(grid)] == [
            ("400.00", "33.00", "433.00"),
            ("400.00", "33.00", "433.00"),
        ]

    def test_each_invoice_carries_its_own_totals(self) -> None:
        grid = build_invoice_grid(
            [
                {"id": 1, "jobId": 7, "total": 10, "items": [{"type": "Service"}]},
                {"id": 2, "jobId": 7, "total": 20, "items": [{"type": "Service"}]},
            ]
        )
        assert [(r["InvoiceId"], r["InvoiceTotal"]) for r in _rows(grid)] == [
            ("1", "10"),
            ("2", "20"),
        ]

    def test_blank_when_the_invoice_record_lacks_them_never_zero(self) -> None:
        grid = build_invoice_grid(
            [
                {"id": 1, "jobId": 2, "items": [{"type": "Service"}]},
                {
                    "id": 3,
                    "jobId": 4,
                    "subTotal": None,
                    "salesTax": None,
                    "total": None,
                    "items": [{"type": "Service"}],
                },
            ]
        )
        cells = [r[c] for r in _rows(grid) for c in _TOTALS]
        assert cells == [""] * 6

    def test_a_genuine_zero_tax_stays_zero(self) -> None:
        grid = build_invoice_grid(
            [{"id": 1, "jobId": 2, "salesTax": 0, "items": [{"type": "Service"}]}]
        )
        assert _rows(grid)[0]["InvoiceSalesTax"] == "0"

    def test_lines_summing_below_the_total_are_represented_faithfully(self) -> None:
        grid = build_invoice_grid(
            [
                {
                    "id": 500,
                    "jobId": 7,
                    "subTotal": "1000.00",
                    "salesTax": "82.50",
                    "total": "1082.50",
                    "items": [
                        {"type": "Equipment", "total": "850.00"},
                        {"type": "Service", "total": "150.00"},
                    ],
                }
            ]
        )
        rows = _rows(grid)
        assert sum(float(r["ItemTotal"]) for r in rows) == 1000.0
        assert {r["InvoiceTotal"] for r in rows} == {"1082.50"}
        assert {r["InvoiceSubTotal"] for r in rows} == {"1000.00"}
        assert {r["InvoiceSalesTax"] for r in rows} == {"82.50"}

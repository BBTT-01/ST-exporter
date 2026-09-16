"""Whole-column-blank detection — the cheap half of the `job_number` bug class.

Three times on this branch the exporter has read a ServiceTitan field by a guessed
name, the fixture has been hand-written using the same guess, and the suite has
gone green while a real tenant's Sheet carried a **whole blank column** (see
`KNOWN_UNVERIFIED.md` and ticket 25). A blank column raises no error and is
indistinguishable at a glance from a contractor who genuinely has no data there,
which is exactly why all three got as far as they did.

So: after a feed builds a grid, look at the grid. If a column is in the header and
**every** data row is blank, across a run big enough that coincidence is implausible,
say so loudly.

This is a DETECTOR, not a guard. It never raises, never changes a grid and never
fails a tab — a genuinely empty column on a real tenant must still export. The only
output is a WARNING naming the tab, the column and the row count.

**And a warning nobody reads is not an output.** The run that carried the 2431
blank rows was GREEN, and a green Actions run is a run whose log nobody opens. So
under Actions the same warning is also emitted as a `::warning` annotation and
appended to the step summary (`logging_setup.announce_to_actions`), which is how
`export.yml` already surfaces the drain notice.
"""

from __future__ import annotations

from st_exporter.logging_setup import announce_to_actions, logger

#: A tab must have at least this many data rows before an all-blank column is
#: reported. The number is a coincidence threshold, not a size preference: if a
#: field is genuinely populated for even one record in ten, seeing 25 consecutive
#: blanks has probability 0.9**25 ~= 7%; at one in two it is ~3e-8. Below 25 rows
#: an all-blank column is an ordinary fact about a small tenant. Above it, a wrong
#: field name is by far the likeliest explanation.
#:
#: It is also low enough to fire on a modest tenant — a week of jobs at one row per
#: assigned technician clears 25 easily — and high enough that the naturally tiny
#: reference tabs (a handful of business units, a dozen pricebook categories) are
#: never reported at all.
MIN_ROWS_FOR_BLANK_COLUMN_WARNING = 25

#: Columns that may legitimately be blank for every row of a real tenant, per tab,
#: each with the reason it is here. **Nothing is exempted silently and nothing is
#: exempted tab-wide**: a column absent from this map is always checked, so the
#: unverified spellings this detector exists to catch (`job_number`,
#: `customer_phone`, `customer_email`, ...) can never be suppressed by accident.
#:
#: `_meta` is not listed because it is not checked at all — it is bookkeeping, not
#: a feed grid, and its `last_cursor` is blank by design on every full-replace feed.
ALL_BLANK_OK: dict[str, dict[str, str]] = {
    "jobs": {
        "summary": "Free-text job summary; many tenants never fill it in.",
    },
    "pricebook.services": {
        "account": ("Optional upstream; frequently unset across a whole catalogue."),
        "add_on_member_price": ("Optional upstream; frequently unset across a whole catalogue."),
        "add_on_price": ("Optional upstream; frequently unset across a whole catalogue."),
        "asset_account": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `assetAccount` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "commission_bonus": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no "
            "`commissionBonus` field, so this column is blank on every row of "
            "every tenant. The three item tabs share one column set (the union "
            "of the three resources' fields) so they can share one parser; a "
            "column a resource does not have is the price of that."
        ),
        "cost": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `cost` field, "
            "so this column is blank on every row of every tenant. The three "
            "item tabs share one column set (the union of the three resources' "
            "fields) so they can share one parser; a column a resource does not "
            "have is the price of that."
        ),
        "cost_of_sale_account": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no "
            "`costOfSaleAccount` field, so this column is blank on every row of "
            "every tenant. The three item tabs share one column set (the union "
            "of the three resources' fields) so they can share one parser; a "
            "column a resource does not have is the price of that."
        ),
        "cross_sale_group": ("Optional upstream; frequently unset across a whole catalogue."),
        "deduct_as_job_cost": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no "
            "`deductAsJobCost` field, so this column is blank on every row of "
            "every tenant. The three item tabs share one column set (the union "
            "of the three resources' fields) so they can share one parser; a "
            "column a resource does not have is the price of that."
        ),
        "description": ("Optional upstream; frequently unset across a whole catalogue."),
        "external_id": ("Optional upstream; frequently unset across a whole catalogue."),
        "image_refs": ("Optional upstream; frequently unset across a whole catalogue."),
        "is_inventory": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `isInventory` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "manufacturer": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `manufacturer` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "manufacturer_warranty_description": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no "
            "`manufacturerWarranty` field, so this column is blank on every row "
            "of every tenant. The three item tabs share one column set (the "
            "union of the three resources' fields) so they can share one "
            "parser; a column a resource does not have is the price of that."
        ),
        "manufacturer_warranty_duration": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no "
            "`manufacturerWarranty` field, so this column is blank on every row "
            "of every tenant. The three item tabs share one column set (the "
            "union of the three resources' fields) so they can share one "
            "parser; a column a resource does not have is the price of that."
        ),
        "member_price": ("Optional upstream; frequently unset across a whole catalogue."),
        "model": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `model` field, "
            "so this column is blank on every row of every tenant. The three "
            "item tabs share one column set (the union of the three resources' "
            "fields) so they can share one parser; a column a resource does not "
            "have is the price of that."
        ),
        "other_vendor_ids": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `otherVendors` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "other_vendor_names": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `otherVendors` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "primary_vendor_cost": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `primaryVendor` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "primary_vendor_id": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `primaryVendor` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "primary_vendor_name": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `primaryVendor` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "primary_vendor_part": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `primaryVendor` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "service_provider_warranty_description": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no "
            "`serviceProviderWarranty` field, so this column is blank on every "
            "row of every tenant. The three item tabs share one column set (the "
            "union of the three resources' fields) so they can share one "
            "parser; a column a resource does not have is the price of that."
        ),
        "service_provider_warranty_duration": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no "
            "`serviceProviderWarranty` field, so this column is blank on every "
            "row of every tenant. The three item tabs share one column set (the "
            "union of the three resources' fields) so they can share one "
            "parser; a column a resource does not have is the price of that."
        ),
        "source": ("Optional upstream; frequently unset across a whole catalogue."),
        "unit_of_measure": (
            "ServiceTitan's Pricebook.V2.ServiceResponse has no `unitOfMeasure` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "warranty_description": ("Optional upstream; frequently unset across a whole catalogue."),
        "warranty_duration": ("Optional upstream; frequently unset across a whole catalogue."),
    },
    "pricebook.equipment": {
        "account": ("Optional upstream; frequently unset across a whole catalogue."),
        "add_on_member_price": ("Optional upstream; frequently unset across a whole catalogue."),
        "add_on_price": ("Optional upstream; frequently unset across a whole catalogue."),
        "asset_account": ("Optional upstream; frequently unset across a whole catalogue."),
        "commission_bonus": ("Optional upstream; frequently unset across a whole catalogue."),
        "cost_of_sale_account": ("Optional upstream; frequently unset across a whole catalogue."),
        "cross_sale_group": ("Optional upstream; frequently unset across a whole catalogue."),
        "deduct_as_job_cost": (
            "ServiceTitan's Pricebook.V2.EquipmentResponse has no "
            "`deductAsJobCost` field, so this column is blank on every row of "
            "every tenant. The three item tabs share one column set (the union "
            "of the three resources' fields) so they can share one parser; a "
            "column a resource does not have is the price of that."
        ),
        "description": ("Optional upstream; frequently unset across a whole catalogue."),
        "external_id": ("Optional upstream; frequently unset across a whole catalogue."),
        "image_refs": ("Optional upstream; frequently unset across a whole catalogue."),
        "is_labor": (
            "ServiceTitan's Pricebook.V2.EquipmentResponse has no `isLabor` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "manufacturer": ("Optional upstream; frequently unset across a whole catalogue."),
        "manufacturer_warranty_description": (
            "Optional upstream; frequently unset across a whole catalogue."
        ),
        "manufacturer_warranty_duration": (
            "Optional upstream; frequently unset across a whole catalogue."
        ),
        "member_price": ("Optional upstream; frequently unset across a whole catalogue."),
        "model": ("Optional upstream; frequently unset across a whole catalogue."),
        "other_vendor_ids": ("Optional upstream; frequently unset across a whole catalogue."),
        "other_vendor_names": ("Optional upstream; frequently unset across a whole catalogue."),
        "primary_vendor_cost": ("Optional upstream; frequently unset across a whole catalogue."),
        "primary_vendor_id": ("Optional upstream; frequently unset across a whole catalogue."),
        "primary_vendor_name": ("Optional upstream; frequently unset across a whole catalogue."),
        "primary_vendor_part": ("Optional upstream; frequently unset across a whole catalogue."),
        "service_provider_warranty_description": (
            "Optional upstream; frequently unset across a whole catalogue."
        ),
        "service_provider_warranty_duration": (
            "Optional upstream; frequently unset across a whole catalogue."
        ),
        "source": ("Optional upstream; frequently unset across a whole catalogue."),
        "unit_of_measure": ("Optional upstream; frequently unset across a whole catalogue."),
        "warranty_description": (
            "ServiceTitan's Pricebook.V2.EquipmentResponse has no `warranty` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "warranty_duration": (
            "ServiceTitan's Pricebook.V2.EquipmentResponse has no `warranty` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
    },
    "pricebook.materials": {
        "account": ("Optional upstream; frequently unset across a whole catalogue."),
        "add_on_member_price": ("Optional upstream; frequently unset across a whole catalogue."),
        "add_on_price": ("Optional upstream; frequently unset across a whole catalogue."),
        "asset_account": ("Optional upstream; frequently unset across a whole catalogue."),
        "commission_bonus": ("Optional upstream; frequently unset across a whole catalogue."),
        "cost_of_sale_account": ("Optional upstream; frequently unset across a whole catalogue."),
        "cross_sale_group": (
            "ServiceTitan's Pricebook.V2.MaterialResponse has no "
            "`crossSaleGroup` field, so this column is blank on every row of "
            "every tenant. The three item tabs share one column set (the union "
            "of the three resources' fields) so they can share one parser; a "
            "column a resource does not have is the price of that."
        ),
        "description": ("Optional upstream; frequently unset across a whole catalogue."),
        "external_id": ("Optional upstream; frequently unset across a whole catalogue."),
        "image_refs": ("Optional upstream; frequently unset across a whole catalogue."),
        "is_labor": (
            "ServiceTitan's Pricebook.V2.MaterialResponse has no `isLabor` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "manufacturer": ("Optional upstream; frequently unset across a whole catalogue."),
        "manufacturer_warranty_description": (
            "ServiceTitan's Pricebook.V2.MaterialResponse has no "
            "`manufacturerWarranty` field, so this column is blank on every row "
            "of every tenant. The three item tabs share one column set (the "
            "union of the three resources' fields) so they can share one "
            "parser; a column a resource does not have is the price of that."
        ),
        "manufacturer_warranty_duration": (
            "ServiceTitan's Pricebook.V2.MaterialResponse has no "
            "`manufacturerWarranty` field, so this column is blank on every row "
            "of every tenant. The three item tabs share one column set (the "
            "union of the three resources' fields) so they can share one "
            "parser; a column a resource does not have is the price of that."
        ),
        "member_price": ("Optional upstream; frequently unset across a whole catalogue."),
        "model": ("Optional upstream; frequently unset across a whole catalogue."),
        "other_vendor_ids": ("Optional upstream; frequently unset across a whole catalogue."),
        "other_vendor_names": ("Optional upstream; frequently unset across a whole catalogue."),
        "primary_vendor_cost": ("Optional upstream; frequently unset across a whole catalogue."),
        "primary_vendor_id": ("Optional upstream; frequently unset across a whole catalogue."),
        "primary_vendor_name": ("Optional upstream; frequently unset across a whole catalogue."),
        "primary_vendor_part": ("Optional upstream; frequently unset across a whole catalogue."),
        "service_provider_warranty_description": (
            "ServiceTitan's Pricebook.V2.MaterialResponse has no "
            "`serviceProviderWarranty` field, so this column is blank on every "
            "row of every tenant. The three item tabs share one column set (the "
            "union of the three resources' fields) so they can share one "
            "parser; a column a resource does not have is the price of that."
        ),
        "service_provider_warranty_duration": (
            "ServiceTitan's Pricebook.V2.MaterialResponse has no "
            "`serviceProviderWarranty` field, so this column is blank on every "
            "row of every tenant. The three item tabs share one column set (the "
            "union of the three resources' fields) so they can share one "
            "parser; a column a resource does not have is the price of that."
        ),
        "source": ("Optional upstream; frequently unset across a whole catalogue."),
        "unit_of_measure": ("Optional upstream; frequently unset across a whole catalogue."),
        "warranty_description": (
            "ServiceTitan's Pricebook.V2.MaterialResponse has no `warranty` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
        "warranty_duration": (
            "ServiceTitan's Pricebook.V2.MaterialResponse has no `warranty` "
            "field, so this column is blank on every row of every tenant. The "
            "three item tabs share one column set (the union of the three "
            "resources' fields) so they can share one parser; a column a "
            "resource does not have is the price of that."
        ),
    },
    "pricebook.categories": {
        "business_unit_ids": ("Optional upstream; frequently unset across a whole catalogue."),
        "description": ("Optional upstream; frequently unset across a whole catalogue."),
        "external_id": ("Optional upstream; frequently unset across a whole catalogue."),
        "image": ("Optional upstream; frequently unset across a whole catalogue."),
        "parent_id": ("Blank at the top level by contract; a flat tree is all blanks."),
        "sku_image_refs": ("Optional upstream; frequently unset across a whole catalogue."),
        "sku_video_refs": ("Optional upstream; frequently unset across a whole catalogue."),
        "source": ("Optional upstream; frequently unset across a whole catalogue."),
    },
    "settings.businessUnits": {
        # NOT a tenant that left a field empty: `code` does not exist on this
        # endpoint at all. `TenantSettings.V2.BusinessUnitResponse` (and its
        # export twin) in `tenant-settings-v2`'s OpenAPI carries no `code`
        # property — the only code-ish fields are `accountCode`/`conceptCode`,
        # which are the franchise account and concept of the TENANT, identical on
        # every business unit, and `certifiedSentriconSpecialistCode`. So no
        # tenant can ever populate this column, which is why run 35132986620
        # (tr-doorservpro) found it blank on all 189 rows.
        #
        # It is exempted rather than repointed because there is nothing correct to
        # point it at, and rather than removed because the column is part of the
        # released `financial.v1` contract. Profit Wizard's reader
        # (`lib/hosted/tabs.ts`, BUSINESS_UNIT_COLUMNS) reads only BusinessUnitId,
        # Name, Address and Active, so nothing downstream is waiting on it:
        # DROP THIS COLUMN at the next `financial.v2` bump.
        "Code": (
            "Absent from the ServiceTitan settings API: BusinessUnitResponse has no "
            "`code` field, so no tenant can populate it. Drop at financial.v2."
        ),
    },
    "payroll.timesheets": {
        "CanceledOn": "Blank unless a job was cancelled; a clean window has none.",
    },
}


def check_blank_columns(
    tab_name: str,
    grid: list[list[str]],
    *,
    min_rows: int = MIN_ROWS_FOR_BLANK_COLUMN_WARNING,
) -> list[str]:
    """Warn about every all-blank column in ``grid``; return their names.

    Never raises: any bug in the scan itself is swallowed, because a detector must
    not be able to fail the run it is watching.
    """
    try:
        return _scan(tab_name, grid, min_rows)
    except Exception:  # pragma: no cover - belt and braces; the scan is total
        logger.debug("blank-column check skipped for %s (internal error)", tab_name, exc_info=True)
        return []


def _scan(tab_name: str, grid: list[list[str]], min_rows: int) -> list[str]:
    if not grid:
        return []
    header, rows = grid[0], grid[1:]
    if len(rows) < min_rows:
        return []

    exempt = ALL_BLANK_OK.get(tab_name, {})
    blank: list[str] = []
    for index, column in enumerate(header):
        if column in exempt:
            continue
        if any(_is_populated(row, index) for row in rows):
            continue
        blank.append(column)
        logger.warning(
            "BLANK COLUMN: %s.%s is empty on all %d rows. That is almost certainly a "
            "wrong ServiceTitan field name in the exporter, not a tenant with no data "
            "— check the field this column is read from against the real API response. "
            "(If it is genuinely optional for every tenant, add it to ALL_BLANK_OK in "
            "st_exporter/blank_columns.py with the reason.)",
            tab_name,
            column,
            len(rows),
        )
        # A warning in the log of a SUCCESSFUL Actions run is invisible: the run is
        # green, nobody opens it, and the 2431-row bug lasted the whole life of the
        # feature for exactly that reason. An annotation shows on the run itself.
        announce_to_actions(
            "Blank column",
            f"{tab_name}.{column} is empty on all {len(rows)} exported rows — almost "
            f"certainly a wrong ServiceTitan field name, not a tenant with no data. "
            f"See st_exporter/blank_columns.py.",
        )
    return blank


def _is_populated(row: list[str], index: int) -> bool:
    """True if ``row`` has a non-whitespace cell at ``index`` (ragged rows tolerated)."""
    return index < len(row) and str(row[index]).strip() != ""

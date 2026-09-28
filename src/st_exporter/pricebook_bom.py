"""Pure record -> row mapping for the three pricebook bill-of-materials tabs.

ServiceTitan links a pricebook item to the SKUs it consumes with three lists on
the item payloads the `pricebook` feed already fetches — no extra request:

- ``serviceMaterials`` and ``serviceEquipment`` on each **service**;
- ``equipmentMaterials`` on each **equipment** item.

Every entry is ``{skuId, quantity}``. That is the whole shape, confirmed
2026-09-28 against a live tenant's ``/pricebook/v2/.../services`` and
``/equipment`` (see ``KNOWN_UNVERIFIED.md``).

**Why these are tabs and not columns on `pricebook.services`.** A CSV cell of sku
ids would read as a usable bill of materials with every quantity silently
dropped, which is why ``pricebook.py`` never emitted them. At its own grain — one
row per entry — the quantity has a cell of its own.

**Why a consumer needs them.** ``Pricebook.V2.ServiceResponse`` has no cost field
at all, so ``pricebook.services.cost`` is blank on every row of every tenant. On
a flat-rate price book the service's material cost IS its linked materials:
``sum(material.cost x quantity)`` over ``pricebook.serviceMaterials``, joined to
``pricebook.materials`` on ``sku_id = st_id``. Without these tabs a consumer can
see a service's hours and its price but never what it costs to deliver.

**One header, one parser, three tabs** — the same rule as the three item tabs:
the tab name says which list a row came from, so ``parent_st_id`` is a service id
on the two ``service*`` tabs and an equipment id on ``pricebook.equipmentMaterials``,
and ``sku_id`` is a material id or an equipment id accordingly.

Cell rules are the item tabs': every cell is text, ``quantity`` is blank when
null (never ``0``), and an entry with no ``skuId`` — or on a parent with no ``id``
— is dropped rather than written with a blank key. Parents are NOT filtered by
``active``: a withdrawn service's links are still its links, and the consumer
joins to the item tab, which already carries ``active``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from st_exporter.format import to_cell_text

CONTRACT_VERSION = "pricebook_bom.v1"

LINK_COLUMNS: tuple[str, ...] = ("parent_st_id", "sku_id", "quantity")

SERVICE_MATERIALS_TAB = "pricebook.serviceMaterials"
SERVICE_EQUIPMENT_TAB = "pricebook.serviceEquipment"
EQUIPMENT_MATERIALS_TAB = "pricebook.equipmentMaterials"

#: Tab -> (the item resource whose payloads carry the list, the list's field name).
#: A link tab is only ever as complete as its parent resource's fetch, so the run
#: writes one only when that resource was read in full (``run._run_pricebook_feed``).
LINK_TABS: dict[str, tuple[str, str]] = {
    SERVICE_MATERIALS_TAB: ("services", "serviceMaterials"),
    SERVICE_EQUIPMENT_TAB: ("services", "serviceEquipment"),
    EQUIPMENT_MATERIALS_TAB: ("equipment", "equipmentMaterials"),
}


def build_link_grid(records: list[dict[str, Any]], field: str) -> list[list[str]]:
    """Header row + one row per ``{skuId, quantity}`` entry in ``record[field]``.

    Rows keep ServiceTitan's order: parents as fetched, entries as listed. No two
    entries were seen sharing a ``skuId`` on one parent, but that is one tenant's
    evidence, not a documented rule — so a repeat is written as two rows rather
    than summed, and the tab promises no row key.
    """
    rows: list[list[str]] = []
    for record in records:
        parent_id = to_cell_text(record.get("id")).strip()
        if not parent_id:
            continue
        entries = record.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            sku_id = to_cell_text(entry.get("skuId")).strip()
            if not sku_id:
                continue
            quantity = entry.get("quantity")
            rows.append([parent_id, sku_id, "" if quantity is None else to_cell_text(quantity)])
    return [list(LINK_COLUMNS)] + rows


def link_grid_builder(field: str) -> Callable[[list[dict[str, Any]]], list[list[str]]]:
    """``build_link_grid`` bound to one list field, in the one-argument shape
    ``contracts.TabContract.build`` takes."""

    def build(records: list[dict[str, Any]]) -> list[list[str]]:
        return build_link_grid(records, field)

    return build


build_service_materials_grid = link_grid_builder("serviceMaterials")
build_service_equipment_grid = link_grid_builder("serviceEquipment")
build_equipment_materials_grid = link_grid_builder("equipmentMaterials")

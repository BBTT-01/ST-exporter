"""Fixture pricebook tenant: one payload per item resource, plus categories.

Deliberately exercises the contract's awkward cells rather than a happy path — a
null price, a withdrawn item, an item in two categories, a repeated asset, an
asset that is an authenticated storage path instead of an HTTPS URL, a child
category, a null cost beside a real ``0`` cost, and a service (which ServiceTitan
gives no cost field at all) beside equipment and materials that have one.

Ticket 04's recorded fixtures can be taken straight off the grid this produces.

**The two `categories` shapes are modelled exactly as ServiceTitan sends them**:
objects on `services` (`SkuCategoryResponse`), bare int64 ids on `equipment` and
`materials` — see `tenant-pricebook-v2`'s OpenAPI and run `35134016237`. Pinning
only the object form here is what let the exporter's object-only reader look
correct while blanking `category_ids`/`category_names` on 15031 live rows.
"""

from __future__ import annotations

import httpx
import respx

TENANT_ID = 12345

SERVICE_1 = {
    "id": 1,
    "code": "SVC-1",
    "displayName": "Annual Tune-Up",
    "description": "Yearly service",
    "price": 129,
    "hours": 1.5,
    "memberPrice": 99,
    "addOnPrice": 79,
    "addOnMemberPrice": None,
    "taxable": True,
    "isLabor": True,
    "paysCommission": True,
    "account": "Service Income",
    "crossSaleGroup": "Maintenance",
    "warranty": {"duration": 12, "description": "One year on the work"},
    "source": "Pricebook",
    "externalId": "EXT-SVC-1",
    # Never exported: an arbitrary bag another integration writes to, and the one
    # field here that could plausibly carry a credential.
    "externalData": [{"key": "legacy_ref", "value": "do-not-publish"}],
    # Never exported: {skuId, quantity}. A CSV of ids would read as a usable bill
    # of materials with every quantity silently dropped.
    "serviceMaterials": [{"skuId": 200, "quantity": 2}],
    "active": True,
    "categories": [{"id": 10, "name": "Service"}],
    "manufacturer": None,
    "model": None,
    "assets": [],
    "modifiedOn": "2026-09-01T00:00:00Z",
}
SERVICE_2_NO_PRICE = {
    "id": 2,
    "code": "SVC-2",
    "displayName": None,
    "name": "Quote On Site",
    "description": None,
    "price": None,
    "hours": None,
    "active": False,
    "categories": [],
    "assets": [],
    "modifiedOn": "2026-09-02T00:00:00Z",
}
EQUIPMENT_1 = {
    "id": 100,
    "code": "DOOR-16x7",
    "displayName": "16x7 Steel Door",
    "description": "Insulated",
    "price": 1299.5,
    "cost": 640,
    "hours": 3,
    "memberPrice": 1199.5,
    "addOnPrice": None,
    "taxable": True,
    "isInventory": False,
    "paysCommission": True,
    "commissionBonus": 25,
    "unitOfMeasure": "Each",
    "crossSaleGroup": "Doors",
    "account": "Equipment Income",
    "costOfSaleAccount": "Cost of Goods Sold",
    "assetAccount": "Inventory Asset",
    # Two DIFFERENT warranties; folding them into one pair of columns would be
    # wrong in a way no consumer could detect.
    "manufacturerWarranty": {"duration": 120, "description": "Ten year on panels"},
    "serviceProviderWarranty": {"duration": 12, "description": "One year on labour"},
    "primaryVendor": {
        "vendorId": 501,
        "vendorName": "Fixture Supply Co",
        "vendorPart": "FS-16x7",
        "cost": 610,
    },
    "otherVendors": [
        {"vendorId": 502, "vendorName": "Second Supply Ltd", "cost": 625},
        # No vendorId: dropped from BOTH columns, so the two stay index-aligned.
        {"vendorName": "Nameless Supply"},
    ],
    "source": "Pricebook",
    "externalId": None,
    "active": True,
    # Bare ids, no names: the real `EquipmentResponse.categories` shape.
    "categories": [10, 11],
    "manufacturer": "Acme",
    "model": "A-16",
    "assets": [
        {"id": "a1", "url": "https://cdn.example.com/a1.jpg", "isDefault": True},
        {"id": "a1", "url": "https://cdn.example.com/a1.jpg"},
        {"id": None, "url": "Images/Pricebook/9f2c-uuid.jpg"},
    ],
    "modifiedOn": "2026-09-03T00:00:00Z",
}
MATERIAL_1 = {
    "id": 200,
    "code": "MAT-1",
    "displayName": "Torsion Spring",
    "description": "",
    "price": 0,
    "cost": 0,
    "hours": None,
    "memberPrice": 0,
    "taxable": False,
    "isInventory": True,
    "deductAsJobCost": True,
    "paysCommission": False,
    "commissionBonus": 0,
    "unitOfMeasure": "Each",
    "account": "Material Income",
    "costOfSaleAccount": "Cost of Goods Sold",
    "assetAccount": None,
    # No vendor at all: every primary_vendor_* column blank, none invented.
    "primaryVendor": None,
    "otherVendors": [],
    "source": "Pricebook",
    "externalId": "EXT-MAT-1",
    "active": True,
    # Bare ids, no names: the real `MaterialResponse.categories` shape.
    "categories": [11],
    "manufacturer": "Acme",
    "model": "TS-1",
    "assets": [],
    "modifiedOn": "2026-09-04T00:00:00Z",
}
CATEGORY_10 = {
    "id": 10,
    "name": "Service",
    "active": True,
    "parentId": None,
    "description": "Labour and maintenance",
    "image": "Images/Pricebook/cat-10.jpg",
    "position": 0,
    "categoryType": "Services",
    "businessUnitIds": [1, 2],
    "skuImages": ["Images/Pricebook/sku-a.jpg", None, "Images/Pricebook/sku-b.jpg"],
    "skuVideos": [],
    "source": "Pricebook",
    "externalId": "EXT-CAT-10",
    # Never exported: parent_id already carries every edge in this tree.
    "subcategories": [{"id": 11, "name": "Doors"}],
}
CATEGORY_11 = {
    "id": 11,
    "name": "Doors",
    "active": True,
    "parentId": 10,
    "categoryType": "Materials",
    "position": 1,
}


def _envelope(data: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"data": data, "hasMore": False})


def register(
    api_base: str,
    *,
    services: list[dict] | None = None,
    equipment: list[dict] | None = None,
    materials: list[dict] | None = None,
    categories: list[dict] | None = None,
) -> None:
    """Register respx routes for the four pricebook list endpoints."""
    payloads = {
        "services": [SERVICE_1, SERVICE_2_NO_PRICE] if services is None else services,
        "equipment": [EQUIPMENT_1] if equipment is None else equipment,
        "materials": [MATERIAL_1] if materials is None else materials,
        "categories": [CATEGORY_10, CATEGORY_11] if categories is None else categories,
    }
    for resource, data in payloads.items():
        respx.get(f"{api_base}/pricebook/v2/tenant/{TENANT_ID}/{resource}").mock(
            return_value=_envelope(data)
        )

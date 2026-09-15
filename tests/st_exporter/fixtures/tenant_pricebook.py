"""Fixture pricebook tenant: one payload per item resource, plus categories.

Deliberately exercises the contract's awkward cells rather than a happy path — a
null price, a withdrawn item, an item in two categories, a repeated asset, an
asset that is an authenticated storage path instead of an HTTPS URL, and a child
category. Ticket 04's recorded fixtures can be taken straight off the grid this
produces.
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
    "active": True,
    "categories": [{"id": 10, "name": "Service"}, {"id": 11, "name": "Doors"}],
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
    "active": True,
    "categories": [{"id": 11, "name": "Doors"}],
    "manufacturer": "Acme",
    "model": "TS-1",
    "assets": [],
    "modifiedOn": "2026-09-04T00:00:00Z",
}
CATEGORY_10 = {"id": 10, "name": "Service", "active": True, "parentId": None}
CATEGORY_11 = {"id": 11, "name": "Doors", "active": True, "parentId": 10}


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

"""Profit Wizard's `push_prices` and `push_estimate` outbox writes.

Payload shapes and ServiceTitan request bodies are copied from Profit
Wizard's own direct-CRM implementation:

- `push_prices`: `lib/crm/index.ts` (`pushToCRM`, ``kind: "push_prices"``)
  enqueues ``{crm_item_id, new_price}`` per item. `lib/crm/servicetitan.ts`'s
  `pushPrices` PATCHes `pricebook/v2/tenant/{id}/services/{id}` when
  `crm_item_id` starts with ``svc_`` (with that prefix stripped) or
  `pricebook/v2/tenant/{id}/materials/{id}` otherwise, body ``{"price": ...}``.
  There is no equipment branch on their side, so there is none here either.

- `push_estimate`: `app/api/servicetitan/push-estimate/route.ts` enqueues
  ``{crmJobId, name, items: [{skuId, description, quantity, price, total}]}``
  — already in the shape `pushEstimate` (`lib/crm/servicetitan.ts`) POSTs to
  `sales/v2/tenant/{id}/estimates` as ``{jobId, name, items}``.
"""

from __future__ import annotations

from typing import Any

from st_cli.client import ServiceTitanClient
from st_exporter.outbox.client import OutboxItem


def perform_push_prices(client: ServiceTitanClient, item: OutboxItem) -> str:
    payload = item.payload
    crm_item_id = payload.get("crm_item_id")
    if not isinstance(crm_item_id, str) or not crm_item_id:
        raise ValueError(f"push_prices needs a non-empty crm_item_id string; got {crm_item_id!r}")
    try:
        new_price = float(payload["new_price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"push_prices needs a numeric new_price; got {payload.get('new_price')!r}"
        ) from exc

    is_service = crm_item_id.startswith("svc_")
    actual_id = crm_item_id[len("svc_") :] if is_service else crm_item_id
    endpoint = "services" if is_service else "materials"

    client.patch("pricebook", f"{endpoint}/{actual_id}", json_body={"price": new_price})
    return actual_id


def _validated_estimate_item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"push_estimate item must be an object; got {raw!r}")

    description = raw.get("description")
    if not isinstance(description, str) or not description:
        raise ValueError(f"push_estimate item needs a non-empty description; got {description!r}")
    try:
        quantity = float(raw["quantity"])
        price = float(raw["price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"push_estimate item needs numeric quantity and price; got {raw!r}"
        ) from exc
    total = raw.get("total")
    try:
        total_value = float(total) if total is not None else quantity * price
    except (TypeError, ValueError) as exc:
        raise ValueError(f"push_estimate item has a non-numeric total; got {total!r}") from exc

    return {
        "skuId": raw.get("skuId"),
        "description": description,
        "quantity": quantity,
        "price": price,
        "total": total_value,
    }


def perform_push_estimate(client: ServiceTitanClient, item: OutboxItem) -> str:
    payload = item.payload
    job_id = payload.get("crmJobId")
    if not job_id:
        raise ValueError(f"push_estimate needs a crmJobId; got {job_id!r}")

    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"push_estimate needs a non-empty name string; got {name!r}")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError(f"push_estimate needs a non-empty items list; got {raw_items!r}")

    items = [_validated_estimate_item(raw) for raw in raw_items]

    body = {"jobId": job_id, "name": name, "items": items}
    created = client.post("sales", "estimates", json_body=body)
    return str(created["id"])

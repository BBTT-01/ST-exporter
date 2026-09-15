"""HTTP client for TradeRated's CRM Outbox — the return lane.

Bearer-authenticated with the per-Company Machine Token (spec.md's "Machine
Token" section); the Company is resolved server-side from the token, never sent
by us. Two calls: claim pending items, report what happened to one.

The exact JSON envelope `GET /crm-outbox` wraps its items in is not specified
in the spec beyond the per-item shape — `{"items": [...]}` is this client's
assumption, flagged in KNOWN_UNVERIFIED.md. If TradeRated's real response
differs, this is the one place to fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from st_exporter.logging_setup import logger
from st_exporter.outbox.routes import TRADERATED_ROUTES, LaneRoutes

_DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class OutboxItem:
    """One claimed item, in the shape every lane's performer reads.

    Each app's claim endpoint returns its own field names — TradeRated's are
    ``id``/``kind``/``payload``, TrueQuote's are ``item_id``/``booking`` and no
    kind at all — so each lane's client is responsible for producing this shape.
    ``extra`` carries whatever else that app sent and the performer needs
    (TrueQuote's ``booking_provider_id``, for instance), so widening one app's
    claim response never means widening this dataclass for all three.
    """

    id: str
    idempotency_key: str
    kind: str
    payload: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)


class TradeRatedOutboxClient:
    """Wraps ``GET {base}/crm-outbox`` and ``POST {base}/crm-outbox/{id}/result``."""

    def __init__(
        self,
        base_url: str,
        machine_token: str,
        routes: LaneRoutes = TRADERATED_ROUTES,
    ) -> None:
        self._routes = routes
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=_DEFAULT_TIMEOUT,
            headers={"Authorization": f"Bearer {machine_token}"},
        )

    def close(self) -> None:
        self._http.close()

    def claim(self, limit: int = 10) -> list[OutboxItem]:
        resp = self._http.get(self._routes.claim_path, params={"limit": limit})
        resp.raise_for_status()
        items = resp.json().get("items") or []
        return [
            OutboxItem(
                id=str(item["id"]),
                idempotency_key=item["idempotency_key"],
                kind=item["kind"],
                payload=item.get("payload") or {},
            )
            for item in items
        ]

    def report_result(
        self,
        item_id: str,
        *,
        status: Literal["succeeded", "failed"],
        st_id: str | None = None,
        error: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"status": status}
        if st_id is not None:
            body["st_id"] = st_id
        if error is not None:
            body["error"] = error
        resp = self._http.post(self._routes.result_path_for(item_id), json=body)
        resp.raise_for_status()


def drop_unidentified(items: list[OutboxItem], product: str) -> list[OutboxItem]:
    """Drop claimed items that carry no id or no idempotency key, loudly.

    **Both fields are identity, and a blank one is worse than a dropped item.**

    - ``idempotency_key`` is half of the ledger's ``(product, key)`` lookup. Two
      items that both carry ``""`` are, to the ledger, the SAME item: the first
      is performed and recorded under ``(product, "")`` and every later one
      looks like a replay and is reported *succeeded, with the first item's
      ServiceTitan id*. A second customer's booking is then reported delivered
      and never created — a silent lost write, the worst outcome this package
      has.
    - ``id`` is what the result is reported against (``.../result/{id}``), so a
      blank one settles nothing in the app's queue even when the write landed.

    Dropping is deliberately not "report it failed": an item with no id has
    nowhere to report *to*. The app still holds the row, its lease expires, and
    it is redelivered — which is right, because nothing was performed. The log
    line is the signal that a claim envelope is being read wrong.
    """
    kept: list[OutboxItem] = []
    for item in items:
        if item.id and item.idempotency_key:
            kept.append(item)
            continue
        logger.warning(
            "%s outbox: dropping a claimed item with a blank %s (id=%r, idempotency_key=%r). "
            "Nothing was written to ServiceTitan for it. A blank idempotency key would "
            "collide with every other blank one in the ledger and report a booking that "
            "was never created as delivered.",
            product,
            "id" if not item.id else "idempotency_key",
            item.id,
            item.idempotency_key,
        )
    return kept

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

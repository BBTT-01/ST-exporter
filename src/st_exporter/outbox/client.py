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

from dataclasses import dataclass
from typing import Any, Literal

import httpx

_DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class OutboxItem:
    id: str
    idempotency_key: str
    kind: str
    payload: dict[str, Any]


class TradeRatedOutboxClient:
    """Wraps ``GET {base}/crm-outbox`` and ``POST {base}/crm-outbox/{id}/result``."""

    def __init__(self, base_url: str, machine_token: str) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=_DEFAULT_TIMEOUT,
            headers={"Authorization": f"Bearer {machine_token}"},
        )

    def close(self) -> None:
        self._http.close()

    def claim(self, limit: int = 10) -> list[OutboxItem]:
        resp = self._http.get("/crm-outbox", params={"limit": limit})
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
        resp = self._http.post(f"/crm-outbox/{item_id}/result", json=body)
        resp.raise_for_status()

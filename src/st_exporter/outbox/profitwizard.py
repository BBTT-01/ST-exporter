"""Profit Wizard's ServiceTitan outbox — claim and report.

    POST {base}/claim     {"limit": n}
    POST {base}/result    one report

``{base}`` is ``https://<host>/api/outbox``. A third path shape, and a third
correct one: TrueQuote's booking queue sits a level deeper because its base
carries other routes (``/pricebook-image``), and TradeRated's id is a path
segment because its base is a Supabase Functions origin. See ``routes.py``.

The Machine Token's scope is ``servicetitan_outbox`` — not TrueQuote's
``booking_outbox``, not TradeRated's ``crm_outbox``. Three apps, three scope
vocabularies, every one of them answering a wrong-scope token with a flat 401.
The company is resolved from the token's own record and never from anything this
client sends.

**Their result contract widens and never narrows**, and it has one consequence
this client has to handle rather than ignore: an **unmatched idempotency key
answers 200 with ``matched: false``**, not a 4xx. That is neither a success nor
a hard failure — the item this exporter just wrote to ServiceTitan was not found
on their side, which is worth a loud log line and is *not* worth abandoning the
rest of the batch over.

**Read the id field, never truthiness.** Their claim RPC returns a composite row
type, and PostgREST serialises "no match" as an object with every field null
rather than as ``null``. A truthiness test on that object is ``True`` — which is
exactly the bug that had Profit Wizard reporting ``matched: true`` for keys that
matched nothing. ``_matched()`` below therefore inspects an identifying field,
and ``_to_item`` drops any claimed row with no id at all.
"""

from __future__ import annotations

from typing import Any

import httpx

from st_cli.client import ServiceTitanClient
from st_exporter.logging_setup import logger
from st_exporter.outbox.actions import UnsupportedOutboxKindError
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.routes import PROFITWIZARD_ROUTES, LaneRoutes

_DEFAULT_TIMEOUT = 30.0

# The four writes Profit Wizard loses under Hosted mode. Naming them here, even
# though none is performed yet, is what makes a claimed item's failure message
# say which write is missing rather than "unknown kind".
KINDS = ("push_estimate", "update_job", "push_prices", "assign_technician")

# Field-name candidates, in the order they are tried. Their contract accepts
# several spellings per field, so their claim response may plausibly use any of
# these; reading all of them costs nothing and is the same
# widen-don't-narrow posture they took on the result side.
_ID_KEYS = ("item_id", "itemId", "id", "outbox_id", "outboxId")
_KEY_KEYS = ("idempotency_key", "idempotencyKey", "key")
_KIND_KEYS = ("kind", "type", "operation")
_PAYLOAD_KEYS = ("payload", "body", "data")


class ProfitWizardOutboxClient:
    """Wraps ``POST {base}/claim`` and ``POST {base}/result``."""

    def __init__(
        self,
        base_url: str,
        machine_token: str,
        routes: LaneRoutes = PROFITWIZARD_ROUTES,
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
        resp = self._http.post(self._routes.claim_path, json={"limit": limit})
        resp.raise_for_status()
        raw_items = _items_of(resp.json())
        items = [_to_item(raw) for raw in raw_items]
        # An all-null composite row is not an item. Dropping it here rather than
        # letting it through is the same defect Profit Wizard hit from the other
        # side: PostgREST serialises "no row" as an object of nulls, which is
        # truthy, so only an identifying FIELD can tell the two apart.
        return [item for item in items if item.id or item.idempotency_key]

    def report_success(self, item: OutboxItem, st_id: str) -> None:
        self._report(item, {"status": "succeeded", "st_id": st_id, "external_id": st_id})

    def report_failure(self, item: OutboxItem, error: str) -> None:
        self._report(item, {"status": "failed", "error": error})

    def _report(self, item: OutboxItem, outcome: dict[str, Any]) -> None:
        body = {
            "item_id": item.id,
            "idempotency_key": item.idempotency_key,
            **outcome,
        }
        resp = self._http.post(self._routes.result_path_for(item.id), json=body)
        resp.raise_for_status()

        if not _matched(resp):
            # 200 + matched:false. Not success: their queue did not move. Not a
            # hard failure either: raising here would abandon every remaining
            # item in the batch over a row that is already written in
            # ServiceTitan and already durable in our own ledger. So: say so,
            # loudly, and carry on.
            logger.warning(
                "profitwizard outbox item %s (key %s): the result was accepted but matched "
                "no queue row (matched=false). The ServiceTitan write already happened and "
                "is recorded in the ledger; their row was not settled.",
                item.id,
                item.idempotency_key,
            )


def _items_of(body: Any) -> list[dict[str, Any]]:
    """The claimed rows, under whichever envelope key carries them."""
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("items", "results", "rows", "data"):
        value = body.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return None


def _to_item(raw: dict[str, Any]) -> OutboxItem:
    payload = _first(raw, _PAYLOAD_KEYS)
    return OutboxItem(
        id=str(_first(raw, _ID_KEYS) or ""),
        idempotency_key=str(_first(raw, _KEY_KEYS) or ""),
        kind=str(_first(raw, _KIND_KEYS) or ""),
        payload=payload if isinstance(payload, dict) else {},
        extra=raw,
    )


def _matched(resp: httpx.Response) -> bool:
    """Did the result report settle a real queue row?

    Defaults to ``True`` for any body that does not say otherwise — an endpoint
    that simply returns ``{}`` or ``204`` must not be read as "nothing matched".
    Only an explicit ``matched: false``, or an explicitly null identifying field,
    counts as unmatched.
    """
    try:
        body = resp.json()
    except ValueError:
        return True
    if not isinstance(body, dict):
        return True

    matched = body.get("matched")
    if isinstance(matched, bool):
        return matched

    # The composite-row shape: a dict is present but every field in it is null.
    # `if body.get("item")` would be True here, which is the trap.
    for key in ("item", "result", "row"):
        nested = body.get(key)
        if isinstance(nested, dict):
            return _first(nested, _ID_KEYS + _KEY_KEYS) is not None
    return True


def perform_profitwizard_item(client: ServiceTitanClient, item: OutboxItem) -> str:
    """Perform one Profit Wizard item. None of the four writes exists yet.

    Ticket 15 ("Profit Wizard: write outbox") owns the four ServiceTitan writes
    — push estimate, update job, push prices, assign technician — and it is
    blocked by this ticket, so the request bodies for them are not knowable
    here. Ticket 12 owns the *lane*: claim, ledger, report, isolation. Those are
    real and exercised.

    Each kind is named individually rather than falling through to "unknown
    kind", so the failure a maintainer reads says which write is missing and
    which ticket owns it — and so a genuinely unrecognised kind still looks
    different from a known-but-unbuilt one.
    """
    if item.kind in KINDS:
        raise UnsupportedOutboxKindError(
            f"{item.kind} is a known Profit Wizard write with no ServiceTitan "
            "implementation in the exporter yet (ticket 15 owns the four write "
            "bodies). The lane claimed and reported it correctly."
        )
    raise UnsupportedOutboxKindError(f"unknown profitwizard outbox kind: {item.kind!r}")

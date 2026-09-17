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

from datetime import datetime
from typing import Any

import httpx

from st_cli.client import ServiceTitanClient
from st_cli.pagination import fetch_all
from st_exporter.logging_setup import logger
from st_exporter.outbox.actions import UnsupportedOutboxKindError
from st_exporter.outbox.client import OutboxItem, drop_unidentified
from st_exporter.outbox.routes import PROFITWIZARD_ROUTES, LaneRoutes

_DEFAULT_TIMEOUT = 30.0

# The four writes Profit Wizard loses under Hosted mode. Naming them here, even
# though only one is performed yet, is what makes a claimed item's failure
# message say which write is missing rather than "unknown kind".
KINDS = ("push_estimate", "update_job", "push_prices", "assign_technician")

# The three still owned by ticket 15, which `perform_profitwizard_item` still
# raises `UnsupportedOutboxKindError` for. `assign_technician` graduated out of
# this tuple once PW's own `crm/index.ts` and `crm/servicetitan.ts` gave a
# knowable payload and endpoint shapes to copy.
_UNIMPLEMENTED_KINDS = ("push_estimate", "update_job", "push_prices")

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
        # truthy, so only an identifying FIELD can tell the two apart. BOTH
        # fields are required, not either: see `drop_unidentified`.
        return drop_unidentified(items, "profitwizard")

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
    for key in ("items", "results", "rows", "data", "jobs"):
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
    """Perform one Profit Wizard item. Three of the four writes still don't exist.

    Ticket 15 ("Profit Wizard: write outbox") owns the four ServiceTitan writes
    — push estimate, update job, push prices, assign technician — and it is
    blocked by this ticket, so the request bodies for the first three are not
    knowable here. `assign_technician` is the exception: PW's own direct-CRM
    implementation (`lib/crm/servicetitan.ts`'s `setAppointmentTechnicianSet`)
    already states its payload and its ServiceTitan endpoints, so that one
    write is performed for real below. Ticket 12 owns the *lane*: claim,
    ledger, report, isolation. Those are real and exercised for all four kinds.

    Each unimplemented kind is named individually rather than falling through
    to "unknown kind", so the failure a maintainer reads says which write is
    missing and which ticket owns it — and so a genuinely unrecognised kind
    still looks different from a known-but-unbuilt one.
    """
    if item.kind == "assign_technician":
        return _perform_assign_technician(client, item)
    if item.kind in _UNIMPLEMENTED_KINDS:
        raise UnsupportedOutboxKindError(
            f"{item.kind} is a known Profit Wizard write with no ServiceTitan "
            "implementation in the exporter yet (ticket 15 owns the write "
            "bodies). The lane claimed and reported it correctly."
        )
    raise UnsupportedOutboxKindError(f"unknown profitwizard outbox kind: {item.kind!r}")


def _is_terminal_appointment_status(status: str) -> bool:
    """Mirrors `setAppointmentTechnicianSet`'s `isTerminalStatus` exactly:
    completed/done/cancelled appointments are dropped from candidacy unless
    every appointment on the job is terminal, in which case there is nothing
    better to pick from."""
    lowered = (status or "").lower()
    return "complet" in lowered or "done" in lowered or "cancel" in lowered


def _resolve_job_appointment_id(
    client: ServiceTitanClient, job_id: int, job_scheduled_start: str | None
) -> int:
    """Pick the one ServiceTitan appointment a job's technician set is written
    to, by the same rule PW's own direct client uses (`setAppointmentTechnicianSet`,
    "Codex #10"): drop terminal appointments unless they are all terminal, then
    take whichever survivor's `start` is closest to `job_scheduled_start` —
    or, with no scheduled start on file (or one that fails to parse), the
    earliest survivor.
    """
    appointments = list(fetch_all(client, "jpm", "appointments", {"jobId": job_id}))
    if not appointments:
        raise ValueError(f"no ServiceTitan appointment found for job {job_id}")

    non_terminal = [
        a for a in appointments if not _is_terminal_appointment_status(a.get("status", ""))
    ]
    candidates = non_terminal or appointments
    if not candidates:
        raise ValueError(
            f"no eligible (non-terminal) ServiceTitan appointment found for job {job_id}"
        )

    target_ms: float | None = None
    if job_scheduled_start:
        try:
            target_ms = _parse_iso_to_epoch_ms(job_scheduled_start)
        except ValueError:
            target_ms = None

    if target_ms is not None:
        chosen = min(
            candidates,
            key=lambda a: abs(_parse_iso_to_epoch_ms(a["start"]) - target_ms),
        )
    else:
        chosen = min(candidates, key=lambda a: a["start"])
    return int(chosen["id"])


def _parse_iso_to_epoch_ms(value: str) -> float:
    """Parse an ISO 8601 timestamp (ServiceTitan's or Profit Wizard's) to epoch
    milliseconds, mirroring `new Date(x).getTime()` closely enough for a
    closest-appointment comparison. Raises ``ValueError`` on anything
    unparsable so callers can fall back to "earliest" the same way
    `Number.isFinite(targetMs)` does on the JS side."""
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    return datetime.fromisoformat(normalized).timestamp() * 1000


def _active_technician_ids(client: ServiceTitanClient, appointment_id: int) -> list[str]:
    """The appointment's current ACTIVE technician ids, in first-seen order.

    Mirrors `readAppointmentActiveTechIds`: only rows with ``active`` true AND
    matching this exact ``appointmentId`` count (the endpoint is queried by id
    but its response shape is not itself scoped to one).
    """
    rows = fetch_all(
        client,
        "dispatch",
        "appointment-assignments",
        {"appointmentIds": str(appointment_id), "active": "True"},
    )
    seen: set[str] = set()
    ids: list[str] = []
    for row in rows:
        if not row.get("active") or row.get("appointmentId") != appointment_id:
            continue
        tech_id = str(row["technicianId"])
        if tech_id in seen:
            continue
        seen.add(tech_id)
        ids.append(tech_id)
    return ids


def _perform_assign_technician(client: ServiceTitanClient, item: OutboxItem) -> str:
    """Write one job's intended technician crew to ServiceTitan.

    The payload is exactly PW's own enqueue at `lib/crm/index.ts` around
    ``kind: "assign_technician"``:
    ``{crmJobId, jobScheduledStart, technicianCrmIds, authorizedRemovalCrmIds}``.
    Everything below mirrors `setAppointmentTechnicianSet` in
    `lib/crm/servicetitan.ts`, PW's own direct-CRM implementation for the same
    write, so a Hosted company and a Direct one land on the same ServiceTitan
    state for the same decision:

    1. Resolve the job's one target appointment (`_resolve_job_appointment_id`).
    2. Read who is currently, actively assigned to it.
    3. `to_add` = intended set minus current; `to_remove` = (current minus
       intended) intersected with `authorized_removal_crm_ids` — NEVER the raw
       difference. A technician a dispatcher added directly in ServiceTitan,
       whom Profit Wizard simply hasn't synced yet, is not "authorized" and
       must never be unassigned by this write. This is the safety-critical
       invariant `assignment-push.ts`'s ``planAuthorizedSetPush`` exists for.
    4. POST the non-empty legs and return the appointment id as ``st_id`` —
       matching `targetCrmId` on PW's own result type.

    Idempotent: an empty `to_add` and an empty `to_remove` make no ServiceTitan
    write at all (step 1 and 2's reads still happen, exactly as PW's own
    ``dryRun``-independent reads do).

    The `unassign-technicians` body shape below (`{jobAppointmentId,
    technicianIds}`) is carried over from PW's own client, which itself carries
    a TODO: ServiceTitan's developer-portal page for it renders client-side and
    was never scraped against a live tenant. `assign-technicians` uses the
    identical shape and IS confirmed live. Treat an unassign failure here as
    unverified against a real ServiceTitan tenant until proven otherwise.
    """
    payload = item.payload
    try:
        job_id = int(payload["crmJobId"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"assign_technician needs an integer crmJobId; got {payload.get('crmJobId')!r}"
        ) from exc

    job_scheduled_start = payload.get("jobScheduledStart")
    intended_raw = payload.get("technicianCrmIds") or []
    authorized_removals = {str(x) for x in (payload.get("authorizedRemovalCrmIds") or [])}

    intended_ids: list[str] = []
    seen_intended: set[str] = set()
    for raw in intended_raw:
        tech_id = str(raw)
        if tech_id in seen_intended:
            continue
        seen_intended.add(tech_id)
        intended_ids.append(tech_id)
    intended_set = set(intended_ids)

    appointment_id = _resolve_job_appointment_id(client, job_id, job_scheduled_start)
    current_ids = _active_technician_ids(client, appointment_id)
    current_set = set(current_ids)

    to_add = [tech_id for tech_id in intended_ids if tech_id not in current_set]
    to_remove = [
        tech_id
        for tech_id in current_ids
        if tech_id not in intended_set and tech_id in authorized_removals
    ]

    if to_add:
        client.post(
            "dispatch",
            "appointment-assignments/assign-technicians",
            json_body={
                "jobAppointmentId": appointment_id,
                "technicianIds": [int(tech_id) for tech_id in to_add],
            },
        )
    if to_remove:
        client.post(
            "dispatch",
            "appointment-assignments/unassign-technicians",
            json_body={
                "jobAppointmentId": appointment_id,
                "technicianIds": [int(tech_id) for tech_id in to_remove],
            },
        )

    return str(appointment_id)

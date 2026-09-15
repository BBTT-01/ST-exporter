"""Write back into the `jobs` tab what the outbox drain just wrote to ServiceTitan.

A dispatcher reassigns a job in Profit Wizard, the drain performs that assignment
against ServiceTitan, and the technician should see it in TradeRated. Without this
module the chain is: queue it, wait for a drain, wait for the **next** jobs feed to
rediscover our own write, then wait for the app to read the Sheet. This module
removes the middle leg: the run that performed the write also updates the rows it
affected, in the same run.

Four properties are load-bearing, and each one is a constraint from ticket 17:

1. **Same grid-replace path.** The updated rows go through ``format.build_job_grid``
   and ``SheetsPort.replace_grid`` — the exact call the jobs feed makes. A reader
   never sees a half-written tab, and the tab's shape cannot drift from the frozen
   ``jobs.v2`` contract, because there is no second row-builder here. There is no
   row-mapping code in this file at all: ``apply_write_backs`` edits the denormalised
   row dicts the jobs feed already produced (see ``denormalize.build_job_rows``) and
   hands them back to the same builder.

2. **No cursor moves.** This writes ONE tab, `jobs`, and nothing else — no `_meta`,
   no raw cache. A cursor belongs to the feed that fetched the data; a write-back
   fetched nothing, so it may not claim to have advanced one. That also means the
   effect of this module is temporary by construction: if it is wrong, or if it
   never runs, the next jobs feed re-derives the tab from the raw cache and
   ServiceTitan's own deltas and overwrites whatever this wrote.

3. **A failed write-back is not a failed item.** By the time this runs, the
   ServiceTitan write has happened, the ledger row is durable, and the item has
   already been reported succeeded to the app — the drain is over. Nothing here can
   reach back and change that, which is the strongest available form of the
   guarantee: it is ordering, not error handling. Reporting the item failed would
   make the app redeliver it and we would perform a **second real write** into a
   contractor's ServiceTitan.

4. **It runs only when the jobs feed ran in the same invocation.** See
   ``JobsWriteBack`` for why that is a structural requirement and not a shortcut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from st_exporter.format import build_job_grid
from st_exporter.logging_setup import logger
from st_exporter.outbox.client import OutboxItem
from st_exporter.sheets import SheetsPort

#: The outbox kinds that change something the `jobs` tab shows. Only Profit
#: Wizard's `assign_technician` qualifies today: the tab's grain is
#: "one row per (appointment, assigned technician)", so an assignment is the one
#: write whose result is already fully described by ids the drain holds.
#:
#: The other kinds are deliberately absent. `referral_lead` creates a CRM lead and
#: `technician_rating` a rating — neither appears in this tab at all. A TrueQuote
#: `booking` creates a whole new job and appointment, whose row needs a customer, a
#: location, a job type and a business unit that only a real fetch can supply;
#: inventing those here would be the "second row-builder" this module exists to
#: avoid, so a new booking waits for the next jobs feed exactly as it does today.
WRITE_BACK_KINDS = frozenset({"assign_technician"})

# Field spellings, each tried in order. Profit Wizard's outbox payload for this
# write is not frozen yet (ticket 15 owns the four write bodies), so this reads
# every spelling its own push code already uses — `lib/crm/assignment-push.ts`
# builds `{jobAppointmentId, technicianIds}` against ServiceTitan's
# assign-technicians / unassign-technicians pair from a `toAdd`/`toRemove` set
# diff — plus the snake_case forms its queue rows use elsewhere. Reading several
# costs nothing and cannot be wrong; reading one and guessing wrong is how
# `job_number` stayed blank on every row ever exported.
_APPOINTMENT_KEYS = (
    "jobAppointmentId",
    "appointment_id",
    "appointmentId",
    "st_appointment_id",
    "servicetitan_appointment_id",
)
_JOB_KEYS = ("job_id", "jobId", "st_job_id", "servicetitan_job_id")
_ADD_KEYS = (
    "technician_ids_to_add",
    "technicianIdsToAdd",
    "to_add",
    "toAdd",
    "assign_technician_ids",
    "assignTechnicianIds",
)
_REMOVE_KEYS = (
    "technician_ids_to_remove",
    "technicianIdsToRemove",
    "to_remove",
    "toRemove",
    "unassign_technician_ids",
    "unassignTechnicianIds",
)
#: The undifferentiated list — `{jobAppointmentId, technicianIds}` is the body of
#: BOTH ServiceTitan calls, so on its own it does not say which one this was. It
#: is read as an assignment unless the payload names the operation (below).
_LIST_KEYS = ("technician_ids", "technicianIds")
_SINGLE_KEYS = (
    "technician_id",
    "technicianId",
    "st_technician_id",
    "servicetitan_technician_id",
)
_OPERATION_KEYS = ("operation", "op", "action", "direction")
_REMOVAL_OPERATIONS = frozenset({"unassign", "remove", "removed", "unassigned", "delete"})


@dataclass(frozen=True)
class AssignmentWriteBack:
    """One appointment's technician change, as ids the drain already holds.

    ``assigned`` and ``unassigned`` are a SET DIFF, not a replacement list —
    Profit Wizard pushes assignments as `toAdd`/`toRemove` against ServiceTitan's
    two endpoints, and the `jobs` tab's grain (one row per assigned technician)
    takes that diff directly. Both may be non-empty for one item: a reassignment
    from tech A to tech B is exactly ``unassigned=("A",), assigned=("B",)``.
    """

    appointment_id: str
    job_id: str = ""
    assigned: tuple[str, ...] = ()
    unassigned: tuple[str, ...] = ()


@dataclass
class WriteBackResult:
    """What ``apply_write_backs`` did, for the log line and the run's summary."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    rows_added: int = 0
    rows_removed: int = 0
    #: Effects whose appointment has no row in this run's jobs output at all —
    #: outside the export window, or its job not yet in the raw cache. NOT an
    #: error: a row cannot be invented without re-fetching the job, the customer
    #: and the location, and inventing one is the second row-builder this module
    #: refuses to be. The next jobs feed picks it up.
    unmatched: list[AssignmentWriteBack] = field(default_factory=list)

    @property
    def rows_changed(self) -> int:
        return self.rows_added + self.rows_removed


def write_back_for_item(product: str, item: OutboxItem) -> AssignmentWriteBack | None:
    """The `jobs`-tab effect of one successfully performed item, or ``None``.

    ``None`` means "nothing in the jobs tab changes because of this item" — the
    normal answer for most kinds. It is never an error: an unreadable payload is
    logged and skipped, because by the time this is called the ServiceTitan write
    has already happened and been reported succeeded, and nothing this function
    decides may cast doubt on that.
    """
    if item.kind not in WRITE_BACK_KINDS:
        return None

    payload = item.payload or {}
    appointment_id = _text(_first(payload, _APPOINTMENT_KEYS))
    assigned, unassigned = _technician_diff(payload)

    if not appointment_id or not (assigned or unassigned):
        logger.warning(
            "%s outbox item %s (%s): the ServiceTitan write SUCCEEDED, but its payload "
            "names no appointment id and/or no technician ids this exporter can read, so "
            "the jobs tab was not updated in this run. The item is NOT failed and is NOT "
            "redelivered; the next jobs feed picks the change up as it always did. "
            "Payload keys seen: %s",
            product,
            item.id,
            item.kind,
            ", ".join(sorted(str(key) for key in payload)) or "(none)",
        )
        return None

    return AssignmentWriteBack(
        appointment_id=appointment_id,
        job_id=_text(_first(payload, _JOB_KEYS)),
        assigned=assigned,
        unassigned=unassigned,
    )


def apply_write_backs(
    rows: list[dict[str, Any]], effects: list[AssignmentWriteBack]
) -> WriteBackResult:
    """Apply ``effects`` to this run's denormalised `jobs` rows. Pure.

    The rows are the ones ``denormalize.build_job_rows`` produced and the jobs feed
    already wrote, so every column but ``st_technician_id`` is carried across from a
    sibling row of the same appointment rather than re-derived. That is what makes a
    written-back row byte-identical to the row the next feed run will produce for it.

    Row ORDER follows the same rule as the denormalise path: within one
    appointment, most-recently-assigned technician first (so a newly assigned one
    leads), and an appointment left with no technicians keeps exactly one row with a
    blank ``st_technician_id`` — the placeholder ``build_job_rows`` emits via its
    ``[None]`` fallback.
    """
    result_rows = [dict(row) for row in rows]
    result = WriteBackResult(rows=result_rows)

    for effect in effects:
        positions = [
            index
            for index, row in enumerate(result_rows)
            if _text(row.get("st_appointment_id")) == effect.appointment_id
        ]
        if not positions:
            result.unmatched.append(effect)
            continue
        if effect.job_id and _text(result_rows[positions[0]].get("st_job_id")) != effect.job_id:
            # The ids disagree, so one of them is about a different thing. Refuse
            # rather than rewrite rows on a guess — the next feed run is correct
            # by construction and is at most one cycle away.
            logger.warning(
                "write-back: appointment %s is on job %s in this run's rows but the outbox "
                "item said job %s. The jobs tab was NOT changed for it; the next jobs feed "
                "will show whatever ServiceTitan actually has.",
                effect.appointment_id,
                _text(result_rows[positions[0]].get("st_job_id")),
                effect.job_id,
            )
            result.unmatched.append(effect)
            continue

        block = [result_rows[index] for index in positions]
        updated, added, removed = _apply_to_block(block, effect)
        if not added and not removed:
            # Nothing about this appointment changed (the technician was already
            # on it, or was never on it). Leave the rows untouched rather than
            # splicing an identical block back in.
            continue

        # Splice the new block in where the old one started. The rows of one
        # appointment are contiguous in practice (build_job_rows sorts by
        # (job, appointment)), but this does not depend on it.
        keep = set(positions)
        start = positions[0]
        rebuilt: list[dict[str, Any]] = []
        for index, row in enumerate(result_rows):
            if index == start:
                rebuilt.extend(updated)
            if index in keep:
                continue
            rebuilt.append(row)
        result_rows = rebuilt
        result.rows = result_rows
        result.rows_added += added
        result.rows_removed += removed

    return result


def _apply_to_block(
    block: list[dict[str, Any]], effect: AssignmentWriteBack
) -> tuple[list[dict[str, Any]], int, int]:
    """One appointment's rows with the effect's diff applied, and how many rows
    that added and removed.

    The counts are ROWS, not technicians, and the two differ in the case that
    matters most: assigning the first technician to an unassigned appointment
    turns the blank placeholder row into a technician row — one row added, one
    removed, net zero. Deriving the counts from the block's LENGTH instead would
    call that "nothing changed" and skip the Sheet write, which is precisely the
    change a dispatcher is waiting to see.
    """
    template = dict(block[0])
    kept = [
        row for row in block if _text(row.get("st_technician_id")) not in set(effect.unassigned)
    ]
    removed = len(block) - len(kept)
    present = {_text(row.get("st_technician_id")) for row in kept}

    added: list[dict[str, Any]] = []
    for technician_id in effect.assigned:
        if technician_id in present:
            continue
        row = dict(template)
        row["st_technician_id"] = technician_id
        added.append(row)
        present.add(technician_id)

    combined = added + kept
    # An appointment with at least one real technician has no placeholder row —
    # `build_job_rows` only emits the blank one when the technician list is empty.
    with_technicians = [row for row in combined if _text(row.get("st_technician_id"))]
    if with_technicians:
        removed += len(combined) - len(with_technicians)
        return with_technicians, len(added), removed

    if not combined and removed:
        # Everybody was unassigned: the appointment keeps exactly the one blank
        # row `build_job_rows` would emit for it.
        placeholder = dict(template)
        placeholder["st_technician_id"] = None
        return [placeholder], 1, removed

    return combined, len(added), removed


class JobsWriteBack:
    """The `jobs` tab of THIS run, held open for the drain that follows it.

    **Why this is a handle rather than a free function, and why it can only exist
    on a run that also ran the jobs feed.**

    The drain runs after the export half, so a write-back needs two things the
    drain does not have: the denormalised rows of this run (the only way to produce
    a row identical to the feed's own, without a second row-builder) and a
    legitimate claim on the `jobs` tab. A drain-only run (`--feeds outbox`) has
    neither. It makes no Export Store round-trip at all — and that is not an
    accident of implementation, it is the stated justification for the outbox
    concurrency lock being SEPARATE from the export one
    (``.github/workflows/export.yml``): a drain-only run "touches no export tab,
    does not read `_meta`, does not write `_meta`". Writing the jobs tab from a
    drain-only run would falsify that sentence and let a drain replace the jobs tab
    while an export run is mid-flight on the other lock — tab and cursor out of
    step, which is the exact class ``fix/cursor-loss`` is already repairing.

    So the honest scope is: write-back happens when `jobs` and `outbox` are named
    in the SAME invocation (`--feeds jobs,outbox`), which takes the shared export
    lock and is therefore serialised against every other export run. A drain-only
    run logs what it deferred and changes nothing. ``docs/examples/
    connector-export.yml`` documents the trade for a contractor's `outbox-drain`
    job: merging it with the (identically scheduled) jobs job buys the write-back
    and costs a place on the shared export lock.
    """

    def __init__(self, export_store: SheetsPort, rows: list[dict[str, Any]]) -> None:
        self._export_store = export_store
        self._rows = rows

    def apply(self, effects: list[AssignmentWriteBack]) -> WriteBackResult:
        """Apply ``effects`` and replace the `jobs` tab. Writes nothing else.

        No `_meta` write, deliberately. `_meta` is written exactly once per run,
        before every side lane, so that nothing running afterwards can strand it;
        a second write here would put that back on the table for a tab correction
        that the next feed run makes anyway. Its `last_cursor` and `last_run_at`
        describe what the FEED fetched and must not be restated by a write-back;
        its `row_count` can therefore be out by the number of rows this changed
        until the next jobs run, which is one cycle and is the cheaper of the two
        inconsistencies.

        **Re-examined against `meta.MetaRowSet`, and DELIBERATELY UNCHANGED.**
        `MetaRowSet` makes a run's `_meta` grid exactly-one-row-per-tab by
        construction, which removes the duplicate-row hazard that used to make
        `last_run_at` parse back to an older run. It does not make a SECOND
        `_meta` write safe, because that hazard was never the one this trade is
        about. Three reasons, in order of weight:

        1. **A `_meta` write is all-tabs, not one tab.** `build_meta_grid` takes
           the whole `MetaRowSet` and `replace_grid` replaces the entire tab, so
           a second write from the write-back lane would have to reproduce every
           OTHER feed's row — including its `last_cursor` — from a `MetaRowSet`
           that lives inside `_run` and is long out of scope by the time the
           drain has finished. Getting that wrong rewinds or strands a cursor for
           feeds this module never touched. `MetaRowSet` guards the assembly of
           one grid; it offers nothing against a second, later assembly.
        2. **It reopens the class the base closed by ordering.** `run.py` puts
           the image pass after the `_meta` write with the instruction never to
           move it back above, precisely so that no side lane can come between a
           written tab and the row describing it. The write-back is a later side
           lane still. Adding a `_meta` write after it means a run can once again
           die mid-`_meta`-replace with every export tab already final — and a
           half-replaced `_meta` loses cursors, which is strictly worse than the
           stale count it would have fixed.
        3. **`last_run_at` would become a lie either way.** Bumped, it claims a
           jobs fetch that did not happen, minutes-to-an-hour after the real one
           (the image pass runs in between). Left alone, the row is the one the
           feed wrote — which is what it should be.

        So the trade stands as recorded: `_meta.jobs.row_count` may disagree with
        the tab by the number of rows a write-back changed, for at most one jobs
        cycle (~5 min), self-correcting on the next jobs run, and never in a
        direction that affects a cursor. `last_run_at` stays truthful about the
        last real fetch.
        """
        result = apply_write_backs(self._rows, effects)
        if not result.rows_changed:
            # Nothing to say to the Sheet. An identical re-write would be a
            # pointless quota spend and a pointless chance to fail.
            return result
        self._export_store.replace_grid("jobs", build_job_grid(result.rows))
        # The in-memory rows become the new truth for any later write-back in
        # this same run, so two effects on one appointment compose.
        self._rows = result.rows
        return result


def _first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _text(value: Any) -> str:
    """One id as the text the tab holds it as. ``None``/blank -> ``""``."""
    if value is None:
        return ""
    return str(value).strip()


def _id_tuple(value: Any) -> tuple[str, ...]:
    """A scalar or a list of ids as a de-duplicated tuple of text ids."""
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple, set)) else [value]
    seen: list[str] = []
    for entry in values:
        text = _text(entry)
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def _technician_diff(payload: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(assigned, unassigned) technician ids from one payload."""
    assigned = _id_tuple(_first(payload, _ADD_KEYS))
    unassigned = _id_tuple(_first(payload, _REMOVE_KEYS))
    if assigned or unassigned:
        return assigned, unassigned

    # No explicit diff: a bare list or scalar. Which side it belongs on is
    # decided by the payload's own operation word if it has one, and defaults to
    # an assignment — `assign_technician` with no direction stated is an assign.
    ids = _id_tuple(_first(payload, _LIST_KEYS)) or _id_tuple(_first(payload, _SINGLE_KEYS))
    if not ids:
        return (), ()
    operation = _text(_first(payload, _OPERATION_KEYS)).lower()
    if operation in _REMOVAL_OPERATIONS:
        return (), ids
    return ids, ()

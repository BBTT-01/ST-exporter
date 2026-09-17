"""The Export Store's published contract: every tab, its columns, its version.

**Why this module exists.** A consumer reads a tab by column NAME. A column that
is renamed, removed or re-ordered does not raise on the consumer's side — it
returns ZERO ROWS, and zero rows is indistinguishable from a contractor who had a
quiet week. That is not hypothetical: ``job_number`` was read as ``number`` and
came back blank on all 2431 rows of a live customer's Sheet for the whole life of
the feature, with every test green, because every fixture on both sides used the
same wrong key.

Four independent codebases now read these tabs — this exporter writes them, and
TradeRated, TrueQuote and Profit Wizard each keep their OWN copy of the reading
code (there is deliberately no shared reader package). What keeps those four in
step is not shared code; it is:

1. a **contract version per feed**, written into `_meta.contract_version` beside
   `exporter_version`, which a consumer checks before parsing; and
2. the **committed fixtures** under ``contracts/fixtures/<contract_version>/``,
   which this repo's tests assert it PRODUCES and every consumer's tests assert
   they can READ.

This module is the single place the exporter declares (1), so a tab's columns and
its version can never be edited in two different files and disagree.
``tests/st_exporter/test_contract_fixtures.py`` is what makes an edit here without
a deliberate regeneration go red.

**What forces a version bump** (the full list — also in ``docs/export-contract.md``):

- a column is renamed, or removed;
- columns are re-ordered (a consumer may read by position);
- a uniqueness/grain rule changes — 0.2.7 turning `jobs` from one row per
  appointment into one row per assigned technician is the canonical example, and
  is why the `jobs` feed is at ``jobs.v2``;
- the meaning of an existing cell changes (e.g. blank starts meaning ``0``).

**What does NOT force a bump**: APPENDING a new column at the end. Consumers look
columns up by name and ignore the rest, so an appended column is additive. Say so
in the CHANGELOG; do not bump.

**How an append survives contact with the published register.** That last rule and
the register used to contradict each other, and the register won: appending a
column changes the bytes of the released version's fixture, and
``scripts/gen_contract_fixtures.py`` refused every byte change alike, so the only
way out it offered was the bump the rule above says not to make. Taking that bump
is not a neutral over-signal — a consumer pinned to the old version MUST answer
``unsupported_contract`` and stop parsing (``docs/export-contract.md``), so
bumping for an appended column takes a tab from "missing one cell" to "dark",
for every consumer, until each of them widens and deploys.

The additive case is therefore recognised rather than refused, and
:func:`appended_columns` is the one place that decides what "additive" means. A
released fixture is **frozen either way**: its bytes on disk, its sha in
``published.json`` and its row_count in the manifest never move, so the register
stays append-only against the base branch and a consumer pinned to that version
keeps testing against exactly the file it was released with. What changes is only
the verdict on a DIFFERENCE — a pure append leaves the released file alone and
says so; a rename, a removal, a re-order, a changed cell or a changed row count
is refused exactly as before.

The consequence worth stating plainly: **an appended column has no committed
fixture** until the next version bump gives the tab a fresh directory. That is
the honest cost of not bumping, and it is bounded by what an appended column IS —
one no consumer of the current version reads, because the current version never
promised it. The producer side is pinned instead by this repo's own unit tests
and, on a live tenant, by ``blank_columns``, which reports a column that is empty
on every row.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable

from st_exporter import financial, pricebook, sales
from st_exporter import format as tab_format

#: A callable that turns a list of source records into the exact grid (header row
#: first) the exporter would write to one tab.
GridBuilder = Callable[[list[dict[str, Any]]], list[list[str]]]


@dataclass(frozen=True)
class TabContract:
    """One output tab: its columns, what one row means, and what makes rows unique."""

    #: The tab name in the Export Store spreadsheet, e.g. ``pricebook.services``.
    name: str
    columns: tuple[str, ...]
    #: One sentence saying what a single row IS. Changing it is a breaking change
    #: even when the columns are untouched — that is exactly what 0.2.7 did.
    grain: str
    #: The column tuple that is unique across the tab, or ``()`` where the tab
    #: makes no uniqueness promise (invoice LINE items do not).
    row_key: tuple[str, ...]
    #: Builds this tab's grid from source records — the same function ``run.py``
    #: calls, so a fixture can never pin a code path the exporter does not use.
    build: GridBuilder


@dataclass(frozen=True)
class FeedContract:
    """One feed: the version string written to `_meta`, and the tabs it owns."""

    feed: str
    version: str
    tabs: tuple[TabContract, ...]


JOBS = FeedContract(
    feed="jobs",
    version=tab_format.JOBS_CONTRACT_VERSION,
    tabs=(
        TabContract(
            name="jobs",
            columns=tab_format.JOB_COLUMNS,
            grain=(
                "one row per (appointment, assigned technician); an appointment "
                "with no assigned technician still emits one row with a blank "
                "st_technician_id"
            ),
            # NOT st_appointment_id alone: 0.2.7 broke that, which is the whole
            # reason this feed is v2.
            row_key=("st_job_id", "st_appointment_id", "st_technician_id"),
            build=tab_format.build_job_grid,
        ),
    ),
)

TECHNICIANS = FeedContract(
    feed="technicians",
    version=tab_format.TECHNICIANS_CONTRACT_VERSION,
    tabs=(
        TabContract(
            name="technicians",
            columns=tab_format.TECHNICIAN_COLUMNS,
            grain="one row per ServiceTitan technician, deduped by st_technician_id",
            row_key=("st_technician_id",),
            build=tab_format.build_technician_grid,
        ),
    ),
)

_ITEM_GRAIN = "one row per pricebook item that has an st_id; id-less records are dropped"

PRICEBOOK = FeedContract(
    feed="pricebook",
    version=pricebook.CONTRACT_VERSION,
    tabs=(
        TabContract(
            name="pricebook.services",
            columns=pricebook.ITEM_COLUMNS,
            grain=_ITEM_GRAIN,
            row_key=("st_id",),
            build=pricebook.build_item_grid,
        ),
        TabContract(
            name="pricebook.equipment",
            columns=pricebook.ITEM_COLUMNS,
            grain=_ITEM_GRAIN,
            row_key=("st_id",),
            build=pricebook.build_item_grid,
        ),
        TabContract(
            name="pricebook.materials",
            columns=pricebook.ITEM_COLUMNS,
            grain=_ITEM_GRAIN,
            row_key=("st_id",),
            build=pricebook.build_item_grid,
        ),
        TabContract(
            name="pricebook.categories",
            columns=pricebook.CATEGORY_COLUMNS,
            grain="one row per pricebook category; parent_id is blank at the top level",
            row_key=("st_id",),
            build=pricebook.build_category_grid,
        ),
    ),
)

FINANCIAL = FeedContract(
    feed="financial",
    version=financial.CONTRACT_VERSION,
    tabs=(
        TabContract(
            name="accounting.invoices",
            columns=financial.INVOICE_COLUMNS,
            grain=(
                "one row per invoice LINE ITEM, not per invoice; an invoice with "
                "no items contributes no rows"
            ),
            # Line items have no id of their own, so this tab promises no key.
            row_key=(),
            build=financial.build_invoice_grid,
        ),
        TabContract(
            name="payroll.timesheets",
            columns=financial.TIMESHEET_COLUMNS,
            grain="one row per job timesheet segment, cancelled segments included",
            row_key=("Id",),
            build=financial.build_timesheet_grid,
        ),
        TabContract(
            name="settings.businessUnits",
            columns=financial.BUSINESS_UNIT_COLUMNS,
            grain="one row per business unit, active and retired alike",
            row_key=("BusinessUnitId",),
            build=financial.build_business_unit_grid,
        ),
        TabContract(
            name="reporting.jobCosts",
            columns=financial.JOB_COST_COLUMNS,
            grain=(
                "one row per Job Costing Summary report row that has a JobNumber; "
                "columns beyond the frozen set are dropped, never appended"
            ),
            row_key=("JobNumber",),
            build=financial.build_job_cost_grid,
        ),
    ),
)

#: `sales.estimates` is a wholly NEW tab, not an appended column on an existing
#: one, so it gets its OWN contract version rather than joining `financial.v1` —
#: adding a tab to an already-published version is refused by
#: `scripts/gen_contract_fixtures.py` (see its module docstring): the register
#: pins the exact set of files released under a version, and only a genuinely
#: NEW version may add one. It is wired into the `financial` FEED's run cadence
#: (``run._run_financial_feed``) even though its contract version differs — the
#: two are independent axes: which run refreshes a tab, and what a consumer
#: pins its reader against.
SALES = FeedContract(
    feed="sales",
    version=sales.CONTRACT_VERSION,
    tabs=(
        TabContract(
            name="sales.estimates",
            columns=sales.ESTIMATE_COLUMNS,
            grain=(
                "one row per estimate ITEM; an estimate with no items still "
                "writes one row with every Item* column blank"
            ),
            row_key=sales.ESTIMATE_KEY_COLUMNS,
            build=sales.build_estimate_grid,
        ),
    ),
)

#: Every feed that writes a versioned tab, in the order they appear in `_meta`.
FEEDS: tuple[FeedContract, ...] = (JOBS, TECHNICIANS, PRICEBOOK, FINANCIAL, SALES)

#: Feed name -> contract version, i.e. exactly what lands in `_meta.contract_version`.
CONTRACT_VERSIONS: dict[str, str] = {contract.feed: contract.version for contract in FEEDS}


def tabs() -> dict[str, tuple[FeedContract, TabContract]]:
    """Tab name -> (its feed, its contract). Every tab the exporter writes except
    `_meta` itself and the private `_raw_*` cache tabs, which are internal
    bookkeeping and not part of the published contract."""
    return {tab.name: (contract, tab) for contract in FEEDS for tab in contract.tabs}


# --- the committed fixture suite ---------------------------------------------
#
# The fixtures are the ONLY mechanism that catches a column rename before deploy,
# now that the four codebases each keep their own reader. Format and layout are
# chosen for one reason: three of the four consumers are TypeScript, so the files
# must be readable without Python. JSON, one file per tab per contract version.

#: Repo-relative root of the committed fixture suite.
FIXTURE_ROOT = "contracts/fixtures"

#: Repo-relative path of the manifest a consumer reads first.
MANIFEST_PATH = f"{FIXTURE_ROOT}/manifest.json"

#: Repo-relative path of the PUBLISHED register: every contract version that has
#: ever been released, and the sha256 of each of its files **as released**.
#:
#: This is the one file in the suite that is not derived from the current code,
#: and that is its entire point. Every other check compares the committed
#: fixtures against what the code produces *today*, so a column rename plus a
#: regeneration leaves every check green and every consumer reading a name that
#: no longer exists — zero rows, four codebases, no error anywhere. The register
#: is the only record of what was actually shipped under `jobs.v2`, so a changed
#: fixture under an already-published version is a fact, not an opinion, and the
#: only way out of it is a NEW version.
PUBLISHED_PATH = f"{FIXTURE_ROOT}/published.json"

#: The command that regenerates the suite. Named in every drift failure message,
#: because the person reading that message did not write this code.
REGENERATE_COMMAND = "python scripts/gen_contract_fixtures.py"

#: How a genuine typo in an already-published fixture is fixed: deliberately, by
#: name, and with a CHANGELOG line, because it rewrites bytes a consumer may
#: already have pinned. It is not a way to land a contract change.
REPUBLISH_FLAG = "--republish"


#: Everything a released fixture payload promises that an APPEND may not move.
#: ``columns`` is absent on purpose — appending to it is the whole point — and is
#: checked by :func:`appended_columns` instead, which is stricter than equality in
#: the direction that matters: the released names must still be the leading
#: columns, in their released order.
_APPEND_FROZEN_KEYS = ("feed", "contract_version", "tab", "grain", "row_key")


def appended_columns(released: Sequence[str], current: Sequence[str]) -> list[str] | None:
    """The columns ``current`` adds to the END of ``released``, or ``None``.

    ``None`` means this is **not** a pure append and must be refused: the released
    names are no longer the leading columns in their released order, which covers
    every renamed, removed and re-ordered column in one test. An empty list means
    the columns are identical.

    Prefix equality is the whole definition, and it is deliberately positional
    rather than set-based. A set comparison would call
    ``(a, b, c) -> (b, a, c, d)`` an append, and a consumer reading by POSITION
    (which ``docs/export-contract.md`` allows for, which is why a re-order is
    breaking) would then read column ``a``'s cells as ``b``.
    """
    released_names = list(released)
    current_names = list(current)
    if current_names[: len(released_names)] != released_names:
        return None
    return current_names[len(released_names) :]


def additive_refusals(released: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Why ``current`` is not a pure APPEND over the released payload ``released``.

    Empty means it is one, and the released file may be left frozen rather than
    rewritten or bumped. This is the single definition of "additive" that both
    ``scripts/gen_contract_fixtures.py`` and
    ``tests/st_exporter/test_contract_fixtures.py`` ask, so the generator can
    never accept something the suite would reject, or the other way round.

    Every row must be unchanged in its released columns and must have grown by
    exactly the appended ones. A changed CELL is therefore still refused — the
    "blank started meaning 0" failure the row-level fixture check exists for is
    not smuggled in under an append — and so is a changed row COUNT, which would
    mean the records behind the fixture moved rather than the schema.
    """
    refusals: list[str] = []
    for key in _APPEND_FROZEN_KEYS:
        if released.get(key) != current.get(key):
            refusals.append(
                f"{key} moved: released {released.get(key)!r}, now {current.get(key)!r} "
                f"— an append may not change it"
            )

    added = appended_columns(released.get("columns") or [], current.get("columns") or [])
    if added is None:
        refusals.append(
            f"the released columns are no longer the leading columns, in order: released "
            f"{list(released.get('columns') or [])}, now {list(current.get('columns') or [])} "
            f"— that is a rename, a removal or a re-order, not an append"
        )
        return refusals
    if not added:
        return refusals

    released_rows = [list(row) for row in released.get("rows") or []]
    current_rows = [list(row) for row in current.get("rows") or []]
    if len(released_rows) != len(current_rows):
        refusals.append(
            f"the row count moved: released {len(released_rows)}, now {len(current_rows)} "
            f"— an append adds columns, never rows"
        )
        return refusals

    width = len(list(released.get("columns") or []))
    for index, (was, now) in enumerate(zip(released_rows, current_rows, strict=False)):
        if now[:width] != was:
            refusals.append(
                f"row {index} changed in its RELEASED columns: {was} -> {now[:width]} "
                f"— an append may add cells, never alter one"
            )
        if len(now) != width + len(added):
            refusals.append(
                f"row {index} is {len(now)} cells wide but the header is "
                f"{width + len(added)} — the grid is malformed"
            )
    return refusals


def narrowed_to_released(released: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """``current`` cut back to the columns ``released`` actually shipped.

    A released fixture describes ONE contract version, and an appended column is
    not part of it. Anything that rewrites released bytes — today that is
    ``--republish`` — must therefore work on this narrowed payload, or the
    appended column rides into a released file under a version stamp that never
    promised it, which is the precise outcome freezing the file exists to
    prevent. Narrowing also keeps the republish guard honest: it compares like
    with like, so an append can neither be mistaken for a structural change nor
    used to smuggle one past a check looking only at cell text.

    Returns ``current`` unchanged when nothing was appended.
    """
    added = appended_columns(released.get("columns") or [], current.get("columns") or [])
    if not added:
        return current
    width = len(list(released.get("columns") or []))
    narrowed = dict(current)
    narrowed["columns"] = list(current.get("columns") or [])[:width]
    narrowed["rows"] = [list(row)[:width] for row in current.get("rows") or []]
    return narrowed


def bump_version(version: str) -> str:
    """``'jobs.v2'`` -> ``'jobs.v3'``; the version a breaking change must move to."""
    feed, _, number = version.rpartition(".v")
    if not feed or not number.isdigit():
        raise ValueError(f"not a contract version of the form '<feed>.v<N>': {version!r}")
    return f"{feed}.v{int(number) + 1}"


def published_bump_hint(version: str) -> str:
    """The one sentence every "you changed a released fixture" message leads with."""
    return f"{version} is published — bump to {bump_version(version)}"


def version_of_fixture(repo_relative_path: str) -> str | None:
    """The contract version directory a suite file sits in, or ``None`` for the
    manifest and the register themselves, which are not versioned."""
    remainder = repo_relative_path.removeprefix(f"{FIXTURE_ROOT}/")
    directory, separator, _ = remainder.partition("/")
    return directory if separator else None


def fixture_relative_path(contract: FeedContract, tab: TabContract) -> str:
    """Where one tab's fixture lives, relative to :data:`FIXTURE_ROOT`.

    Keyed by contract VERSION, not by feed: when `jobs.v3` arrives, the `jobs.v2`
    directory stays exactly as it is, so a consumer still pinned to v2 keeps a
    fixture to test itself against instead of losing its only reference.
    """
    return f"{contract.version}/{tab.name}.json"


def fixture_payload(
    contract: FeedContract, tab: TabContract, grid: list[list[str]]
) -> dict[str, Any]:
    """The exact JSON document committed for one tab.

    ``columns`` is the header row and ``rows`` the data rows, every cell a string,
    exactly as the exporter writes them to Sheets. ``grain`` and ``row_key`` are
    carried too: a consumer that reads every column correctly can still be wrong
    about what a row IS (`jobs.v1` -> `jobs.v2` changed nothing but that).
    """
    header, *rows = grid
    if list(header) != list(tab.columns):
        raise ValueError(
            f"{tab.name}: grid header {header} is not the declared column set {list(tab.columns)}"
        )
    return {
        "feed": contract.feed,
        "contract_version": contract.version,
        "tab": tab.name,
        "grain": tab.grain,
        "row_key": list(tab.row_key),
        "columns": list(tab.columns),
        "rows": [list(row) for row in rows],
    }

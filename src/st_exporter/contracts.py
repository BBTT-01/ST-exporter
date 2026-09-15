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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from st_exporter import financial, pricebook
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

#: Every feed that writes a versioned tab, in the order they appear in `_meta`.
FEEDS: tuple[FeedContract, ...] = (JOBS, TECHNICIANS, PRICEBOOK, FINANCIAL)

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

"""The producer half of the contract guard: the exporter still writes the fixtures.

Four codebases read the Export Store and each keeps its own copy of the reading
code. Nothing shared prevents a column rename from reaching production — a wrong
column name raises nothing on a consumer, it returns ZERO ROWS, and zero rows
looks exactly like a contractor who had a quiet week. ``job_number`` was blank on
all 2431 rows of a live Sheet for the whole life of the feature with every test
green, because every fixture on both sides used the same wrong key.

What this file does is narrow and load-bearing: it asserts the grids the exporter
produces today are byte-for-byte the ones committed under ``contracts/fixtures/``.
The three consuming apps assert they can READ those same files. A rename on
either side then fails in CI, on the side that made it, before deploy.

The failure messages are written for someone who did NOT write this code — they
name the tab, the column, and the two legitimate ways out.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

from st_exporter import contracts
from tests.st_exporter.fixtures.contract_cases import SOURCE_RECORDS

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / contracts.FIXTURE_ROOT
MANIFEST = REPO_ROOT / contracts.MANIFEST_PATH

_TAB_CONTRACTS = contracts.tabs()
_TAB_NAMES = sorted(_TAB_CONTRACTS)


def _generator() -> Any:
    """The fixture generator, loaded from ``scripts/`` (not an installed package)."""
    path = REPO_ROOT / "scripts" / "gen_contract_fixtures.py"
    spec = importlib.util.spec_from_file_location("gen_contract_fixtures", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(tab_name: str) -> dict[str, Any]:
    contract, tab = _TAB_CONTRACTS[tab_name]
    path = FIXTURE_ROOT / contracts.fixture_relative_path(contract, tab)
    assert path.exists(), (
        f"MISSING CONTRACT FIXTURE — tab '{tab_name}' (contract {contract.version}) has no "
        f"committed fixture at {path.relative_to(REPO_ROOT)}.\n"
        f"Every tab the exporter writes must have one: it is the only thing the three "
        f"consuming apps can test themselves against.\n"
        f"Create it with: {contracts.REGENERATE_COMMAND}"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _what_to_do(tab_name: str, contract: contracts.FeedContract) -> str:
    return (
        f"\nTwo ways out, and only two:\n"
        f"  1. The change was NOT intended — revert it. A consumer reading "
        f"'{tab_name}' by column name would have silently returned zero rows.\n"
        f"  2. The change WAS intended — it is a BREAKING contract change:\n"
        f"       a. bump the '{contract.feed}' contract version "
        f"(currently {contract.version}) — see src/st_exporter/contracts.py;\n"
        f"       b. regenerate the fixtures: {contracts.REGENERATE_COMMAND};\n"
        f"       c. tell TradeRated, TrueQuote and Profit Wizard — each keeps its "
        f"own reader and each must widen its supported range.\n"
        f"Full rules: docs/export-contract.md"
    )


@pytest.mark.parametrize("tab_name", _TAB_NAMES)
def test_tab_columns_match_the_committed_fixture(tab_name: str) -> None:
    """A renamed, removed, added or re-ordered column fails here, by name."""
    contract, tab = _TAB_CONTRACTS[tab_name]
    committed = _load(tab_name)["columns"]
    current = list(tab.columns)
    if current == committed:
        return

    renamed = [
        f"position {i}: fixture says '{was}', the code now writes '{now}'"
        for i, (was, now) in enumerate(zip(committed, current, strict=False))
        if was != now
    ]
    dropped = [name for name in committed if name not in current]
    added = [name for name in current if name not in committed]
    detail = "\n".join(
        f"  {line}"
        for line in (
            *renamed,
            *(f"column '{name}' is GONE from the code" for name in dropped),
            *(f"column '{name}' is NEW in the code" for name in added),
        )
    )
    pytest.fail(
        f"CONTRACT DRIFT — tab '{tab_name}' (contract {contract.version}) no longer "
        f"has the columns its committed fixture pins:\n{detail}\n{_what_to_do(tab_name, contract)}",
        pytrace=False,
    )


@pytest.mark.parametrize("tab_name", _TAB_NAMES)
def test_tab_rows_match_the_committed_fixture(tab_name: str) -> None:
    """The exporter still produces the exact cells the fixture pins.

    Cell-level, not just header-level, because the failures this defends against
    are as often "blank started meaning 0" as "the column was renamed", and a
    header check would sail straight past that.
    """
    contract, tab = _TAB_CONTRACTS[tab_name]
    committed = _load(tab_name)
    produced = tab.build(SOURCE_RECORDS[tab_name])[1:]
    expected = [list(row) for row in committed["rows"]]
    if produced == expected:
        return

    columns = committed["columns"]
    differences: list[str] = []
    for index in range(max(len(produced), len(expected))):
        was = expected[index] if index < len(expected) else None
        now = produced[index] if index < len(produced) else None
        if was == now:
            continue
        if was is None:
            differences.append(f"  row {index}: EXTRA row produced: {now}")
        elif now is None:
            differences.append(f"  row {index}: row MISSING, fixture has: {was}")
        else:
            for column_index, name in enumerate(columns):
                old = was[column_index] if column_index < len(was) else "<absent>"
                new = now[column_index] if column_index < len(now) else "<absent>"
                if old != new:
                    differences.append(
                        f"  row {index}, column '{name}': fixture {old!r} -> code {new!r}"
                    )
    pytest.fail(
        f"CONTRACT DRIFT — tab '{tab_name}' (contract {contract.version}) no longer "
        f"produces the cells its committed fixture pins:\n"
        + "\n".join(differences[:40])
        + _what_to_do(tab_name, contract),
        pytrace=False,
    )


@pytest.mark.parametrize("tab_name", _TAB_NAMES)
def test_tab_grain_and_row_key_match_the_committed_fixture(tab_name: str) -> None:
    """A grain or uniqueness change with the columns untouched still fails.

    This is the 0.2.7 failure exactly: the `jobs` column set did not move an inch,
    but a row stopped meaning "an appointment" and started meaning "a technician
    on an appointment", ``st_appointment_id`` stopped being unique, and a consumer
    broke in production with nothing to warn it.
    """
    contract, tab = _TAB_CONTRACTS[tab_name]
    committed = _load(tab_name)
    if committed["grain"] == tab.grain and committed["row_key"] == list(tab.row_key):
        return
    pytest.fail(
        f"CONTRACT DRIFT — tab '{tab_name}' (contract {contract.version}) changed what a "
        f"ROW MEANS, even though its columns may be untouched:\n"
        f"  grain:   fixture {committed['grain']!r}\n"
        f"           code    {tab.grain!r}\n"
        f"  row_key: fixture {committed['row_key']} -> code {list(tab.row_key)}\n"
        + _what_to_do(tab_name, contract),
        pytrace=False,
    )


def test_every_tab_the_exporter_writes_has_a_fixture() -> None:
    """A new tab cannot ship without a fixture for the three consumers to read."""
    missing = sorted(set(_TAB_CONTRACTS) - set(SOURCE_RECORDS))
    assert not missing, (
        f"NEW TAB WITHOUT A FIXTURE — {missing} are declared in "
        f"st_exporter/contracts.py but have no source records in "
        f"tests/st_exporter/fixtures/contract_cases.py, so no fixture is generated "
        f"for them and no consumer can test itself against them.\n"
        f"Add records covering that tab's awkward cells, then run: "
        f"{contracts.REGENERATE_COMMAND}"
    )


def test_manifest_declares_the_versions_the_code_writes() -> None:
    """`_meta.contract_version` and the fixture directory names cannot disagree."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    declared = {feed: entry["contract_version"] for feed, entry in manifest["feeds"].items()}
    assert declared == contracts.CONTRACT_VERSIONS, (
        f"CONTRACT VERSION MISMATCH — the committed manifest says {declared} but the "
        f"exporter writes {contracts.CONTRACT_VERSIONS} into _meta.contract_version.\n"
        f"A consumer checks the _meta value against the fixtures it was tested on, so "
        f"these two must never differ.\n"
        f"Regenerate deliberately: {contracts.REGENERATE_COMMAND}"
    )


def test_manifest_checksums_match_the_committed_fixtures() -> None:
    """A hand-edited fixture is caught here rather than trusted.

    The checksums are also what lets a consumer vendor a copy and still prove it
    is the pinned one — see docs/export-contract.md.
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for feed, entry in manifest["feeds"].items():
        for tab_name, tab_entry in entry["tabs"].items():
            path = FIXTURE_ROOT / tab_entry["path"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == tab_entry["sha256"], (
                f"FIXTURE EDITED BY HAND — {tab_entry['path']} ({feed}/{tab_name}) does not "
                f"match the sha256 in the manifest. Fixtures are generated, never edited: "
                f"change the source records in tests/st_exporter/fixtures/contract_cases.py "
                f"and run {contracts.REGENERATE_COMMAND}."
            )


def test_committed_suite_is_exactly_what_the_generator_produces() -> None:
    """Belt and braces over the per-tab checks: nothing in the suite is stale."""
    files = _generator().build_files()
    stale = [
        relative
        for relative, text in sorted(files.items())
        if (REPO_ROOT / relative).read_text(encoding="utf-8") != text
    ]
    assert not stale, (
        f"STALE CONTRACT FIXTURES — {stale} no longer match what the exporter produces.\n"
        f"If the change was intended, bump the affected feed's contract version and run "
        f"{contracts.REGENERATE_COMMAND}. If it was not, revert it: every consumer reads "
        f"these tabs by column name and would have returned zero rows."
    )


# --- the public-repo guard ----------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+")
_PHONE = re.compile(r"\b\d{3}[- ]\d{4}\b")
#: Reserved for documentation and testing, and guaranteed never to resolve to a
#: real mailbox: RFC 2606 (`.example`/`example.com`) and RFC 6761 (`.invalid`).
_SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example.invalid")
#: The North American fictitious-number block (555-01xx), the phone equivalent.
_SAFE_PHONE_PREFIX = "555-0"


def _fixture_files() -> list[Path]:
    return sorted(FIXTURE_ROOT.rglob("*.json"))


def test_no_fixture_carries_a_routable_email_address() -> None:
    """``BBTT-01/ST-exporter`` is PUBLIC. A fixture recorded from a real tenant and
    committed unscrubbed publishes a contractor's customer list — permanently, and
    to everyone. This is the mechanical half of the scrubbing rule in
    docs/export-contract.md; the human half is that real responses are scrubbed
    BEFORE they ever reach a branch."""
    offenders: list[str] = []
    for path in _fixture_files():
        for address in _EMAIL.findall(path.read_text(encoding="utf-8")):
            if not address.lower().endswith(_SAFE_EMAIL_DOMAINS):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {address}")
    assert not offenders, (
        "REAL-LOOKING EMAIL IN A PUBLIC FIXTURE:\n  "
        + "\n  ".join(offenders)
        + "\nFixtures may only use reserved domains "
        + f"({', '.join(_SAFE_EMAIL_DOMAINS)}), which cannot route to a real mailbox."
    )


def test_no_fixture_carries_a_dialable_phone_number() -> None:
    offenders: list[str] = []
    for path in _fixture_files():
        text = path.read_text(encoding="utf-8")
        for number in _PHONE.findall(text):
            if _SAFE_PHONE_PREFIX not in number and not number.startswith("555-"):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {number}")
    assert not offenders, (
        "REAL-LOOKING PHONE NUMBER IN A PUBLIC FIXTURE:\n  "
        + "\n  ".join(offenders)
        + "\nFixtures may only use the 555-01xx fictitious block."
    )


def test_manifest_asserts_the_suite_carries_no_real_customer_data() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["contains_real_customer_data"] is False

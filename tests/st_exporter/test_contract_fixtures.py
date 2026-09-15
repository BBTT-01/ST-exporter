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
import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
    """The instruction this message leads with is BUMP THE VERSION.

    It used to lead with "regenerate", and regeneration is the bypass: it rewrites
    the very files four codebases test themselves against, leaves the version stamp
    saying nothing changed, and turns a loud failure into a silent zero-row one in
    production. Regeneration is what you do AFTER bumping — and since the register
    landed, the generator refuses to do it any other way round.
    """
    return (
        f"\nTwo ways out, and only two:\n"
        f"  1. The change was NOT intended — revert it. A consumer reading "
        f"'{tab_name}' by column name would have silently returned zero rows.\n"
        f"  2. The change WAS intended — then it is a BREAKING contract change, and "
        f"the FIRST step is the version, not the fixtures:\n"
        f"       a. BUMP the '{contract.feed}' contract version — "
        f"{contracts.published_bump_hint(contract.version)} "
        f"(see src/st_exporter/contracts.py);\n"
        f"       b. only THEN regenerate: {contracts.REGENERATE_COMMAND}. Run before "
        f"the bump it refuses, because {contract.version} is already released;\n"
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


# --- what was PUBLISHED, not what the code happens to produce today ------------
#
# Every check above compares the committed fixtures to the CURRENT code. That is
# a check that a regeneration always satisfies — and regenerating is exactly what
# a developer does when one of them goes red. The reviewer's repro: rename
# `job_number` to `job_no`, watch two tests fail, run the generator, watch 37
# pass, with `jobs.v2` still in the manifest and `job_no` in the fixture. Every
# consumer's version check then passes and every consumer reads a column that no
# longer exists — zero rows, four codebases, no error.
#
# `contracts/fixtures/published.json` is the only file here that is not derived
# from the code: it is the sha256 of every fixture AS RELEASED. These two tests
# read it directly (no generator, no rebuild) so that no amount of regenerating
# can make them pass.


def _published() -> dict[str, dict[str, str]]:
    path = REPO_ROOT / contracts.PUBLISHED_PATH
    assert path.exists(), (
        f"MISSING PUBLISHED REGISTER — {contracts.PUBLISHED_PATH} is what records the "
        f"bytes each contract version was released with. Without it every check in this "
        f"file is satisfied by regenerating, which is the bypass, not the fix."
    )
    versions: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8"))["versions"]
    return versions


@pytest.mark.parametrize("version", sorted(_published()))
def test_a_published_contract_version_is_byte_identical_to_what_was_released(
    version: str,
) -> None:
    """A changed fixture under an already-released version is a version bump, full stop."""
    directory = FIXTURE_ROOT / version
    released = _published()[version]
    assert directory.is_dir(), (
        f"PUBLISHED VERSION DIRECTORY IS GONE — {version} was released and consumers "
        f"pinned to it test themselves against {contracts.FIXTURE_ROOT}/{version}/. "
        f"Deleting it takes away their only reference; old versions stay forever."
    )
    on_disk = sorted(path.name for path in directory.glob("*.json"))
    assert on_disk == sorted(released), (
        f"PUBLISHED VERSION CHANGED SHAPE — {version} was released with {sorted(released)} "
        f"but now holds {on_disk}.\n"
        f"{contracts.published_bump_hint(version)}."
    )
    for name, sha in sorted(released.items()):
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        assert digest == sha, (
            f"A RELEASED FIXTURE CHANGED — {version}/{name} does not match the bytes it "
            f"was published with.\n"
            f"  {contracts.published_bump_hint(version)}\n"
            f"TradeRated, TrueQuote and Profit Wizard each keep their own reader and each "
            f"tests itself against exactly this file. Rewriting it in place ships a "
            f"different tab under a version stamp that says nothing changed: every "
            f"consumer's version check passes and every consumer reads the old column "
            f"name and gets ZERO ROWS. That is the job_number incident again, wearing a "
            f"version stamp that says it is fine.\n"
            f"Regenerating does NOT fix this — it is what causes it. Bump "
            f"{version} to {contracts.bump_version(version)} in src/st_exporter/contracts.py "
            f"(or the module it reads the version from), then regenerate: the new directory "
            f"is written and this one is left alone.\n"
            f"A genuine typo in a released fixture: add a CHANGELOG line naming {version} "
            f"and 'republish', then {contracts.REGENERATE_COMMAND} "
            f"{contracts.REPUBLISH_FLAG} {version}."
        )


def test_every_version_directory_in_the_suite_is_registered_as_published() -> None:
    """The register cannot be dodged by deleting the entry either.

    An unregistered version directory is a directory nothing freezes: it would be
    rewritable in place forever, which is the hole this whole mechanism closes.
    """
    directories = sorted(
        path.name
        for path in FIXTURE_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    unregistered = sorted(set(directories) - set(_published()))
    assert not unregistered, (
        f"UNREGISTERED CONTRACT VERSION — {unregistered} exist under "
        f"{contracts.FIXTURE_ROOT}/ but are not listed in {contracts.PUBLISHED_PATH}, so "
        f"nothing pins the bytes they were released with and they can be rewritten in "
        f"place. Run {contracts.REGENERATE_COMMAND} to register them."
    )


def test_the_version_the_code_writes_has_a_published_directory() -> None:
    published = _published()
    missing = {
        feed: version
        for feed, version in contracts.CONTRACT_VERSIONS.items()
        if version not in published
    }
    assert not missing, (
        f"CONTRACT VERSION WITH NO REGISTERED FIXTURES — {missing}. A feed writes this "
        f"version into _meta.contract_version, so a consumer will look for fixtures under "
        f"it. Run {contracts.REGENERATE_COMMAND}."
    )


# --- the generator's half of the same guard -----------------------------------
#
# The tests above catch a released fixture that has already been changed. These
# catch the act: the generator must REFUSE to write it in the first place, so the
# developer meets the rule at the moment they would otherwise have bypassed it.


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    """The real generator, pointed at a disposable copy of the committed suite."""
    module = _generator()
    shutil.copytree(FIXTURE_ROOT, tmp_path / contracts.FIXTURE_ROOT)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    return module, tmp_path


def _run(module: Any, *argv: str) -> int:
    with patch.object(sys, "argv", ["gen_contract_fixtures.py", *argv]):
        exit_code: int = module.main()
    return exit_code


def _tamper_with_the_released_jobs_sha(root: Path) -> None:
    """Stand-in for "the code now produces different bytes under jobs.v2": the
    register says one thing and what would be written is another, which is the
    same comparison from the other end."""
    path = root / contracts.PUBLISHED_PATH
    document = json.loads(path.read_text(encoding="utf-8"))
    document["versions"]["jobs.v2"]["jobs.json"] = "0" * 64
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


class TestTheGeneratorRefusesToRewriteAPublishedVersion:
    def test_it_exits_non_zero_and_names_the_version_to_bump(
        self, sandbox: tuple[Any, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        module, root = sandbox
        _tamper_with_the_released_jobs_sha(root)

        assert _run(module) == 2

        printed = capsys.readouterr().out
        assert "jobs.v2 is published — bump to jobs.v3" in printed
        assert "REFUSING" in printed
        # And it wrote nothing: the released file is untouched.
        released = (root / contracts.FIXTURE_ROOT / "jobs.v2" / "jobs.json").read_bytes()
        assert hashlib.sha256(released).hexdigest() != "0" * 64

    def test_check_mode_refuses_too(self, sandbox: tuple[Any, Path]) -> None:
        """CI runs `--check`; it must not be the lenient path."""
        module, root = sandbox
        _tamper_with_the_released_jobs_sha(root)
        assert _run(module, "--check") == 2

    def test_republish_without_a_changelog_line_is_still_refused(
        self, sandbox: tuple[Any, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        module, root = sandbox
        _tamper_with_the_released_jobs_sha(root)

        assert _run(module, "--republish", "jobs.v2") == 2
        assert "republish" in capsys.readouterr().out

    def test_republish_with_a_changelog_line_rewrites_that_version_only(
        self, sandbox: tuple[Any, Path]
    ) -> None:
        """The escape hatch for a genuine typo in a released fixture: deliberate,
        named, and leaving a line a consumer can find."""
        module, root = sandbox
        _tamper_with_the_released_jobs_sha(root)
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n- republish jobs.v2: fixed a transposed digit in a fake phone\n",
            encoding="utf-8",
        )

        assert _run(module, "--republish", "jobs.v2") == 0

        register = json.loads((root / contracts.PUBLISHED_PATH).read_text(encoding="utf-8"))
        released = (root / contracts.FIXTURE_ROOT / "jobs.v2" / "jobs.json").read_bytes()
        assert register["versions"]["jobs.v2"]["jobs.json"] == hashlib.sha256(released).hexdigest()

    def test_deleting_the_register_does_not_rebuild_it_from_todays_code(
        self, sandbox: tuple[Any, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Otherwise the bypass is just one `rm` long: delete the register,
        regenerate, and every released version is re-baselined in silence."""
        module, root = sandbox
        (root / contracts.PUBLISHED_PATH).unlink()

        assert _run(module) == 2
        assert "IS MISSING" in capsys.readouterr().out
        assert not (root / contracts.PUBLISHED_PATH).exists()

    def test_an_untouched_suite_regenerates_cleanly(self, sandbox: tuple[Any, Path]) -> None:
        """The guard must be invisible when nothing changed — a check that cries
        wolf on every run is a check that gets deleted."""
        module, _root = sandbox
        assert _run(module) == 0
        assert _run(module, "--check") == 0


# --- the row_key is a promise about the ROWS, not a string in a file ----------


@pytest.mark.parametrize("tab_name", _TAB_NAMES)
def test_the_committed_rows_are_actually_unique_under_their_row_key(tab_name: str) -> None:
    """`row_key` is asserted against the rows, independently of regeneration.

    Comparing the fixture's `row_key` to `contracts.py` only proves the fixture was
    generated from `contracts.py`, which it was. This checks the thing the key
    actually claims — that no two rows share it — so reverting the grain and
    regenerating cannot stay green.
    """
    committed = _load(tab_name)
    row_key = committed["row_key"]
    if not row_key:
        return  # a tab may promise no key at all (invoice LINE items do not)
    columns = committed["columns"]
    indices = [columns.index(name) for name in row_key]
    seen: dict[tuple[str, ...], int] = {}
    for index, row in enumerate(committed["rows"]):
        key = tuple(row[i] for i in indices)
        if key in seen:
            pytest.fail(
                f"ROW KEY IS NOT UNIQUE — tab '{tab_name}' declares {row_key} as its "
                f"row_key, but committed rows {seen[key]} and {index} both have {list(key)}.\n"
                f"Either the rows changed grain (a BREAKING change: bump the version) or "
                f"the declared row_key is wrong. A consumer keys its own store on this "
                f"tuple; a duplicate silently overwrites one of the two rows.",
                pytrace=False,
            )
        seen[key] = index


def test_the_jobs_fixture_proves_st_appointment_id_alone_is_not_the_key() -> None:
    """The 0.2.7 failure, asserted on the ROWS.

    0.2.7 renamed nothing and changed only uniqueness: `jobs` went from one row per
    appointment to one row per assigned technician, `st_appointment_id` stopped
    being unique, and a consumer keyed on it broke in production. So the fixture
    must CONTAIN that collision — a fixture where every appointment happens to
    carry one technician would let the old grain come back unnoticed.
    """
    committed = _load("jobs")
    index = committed["columns"].index("st_appointment_id")
    appointment_ids = [row[index] for row in committed["rows"]]
    collisions = len(appointment_ids) - len(set(appointment_ids))
    assert collisions >= 1, (
        "THE jobs FIXTURE NO LONGER PROVES ITS OWN GRAIN — every committed row has a "
        "distinct st_appointment_id, so a consumer keyed the jobs.v1 way (one row per "
        "appointment) would pass against it, and reverting jobs to the v1 grain would "
        "not fail anything. The fixture must keep at least one appointment carrying two "
        "assigned technicians: see tests/st_exporter/fixtures/contract_cases.py."
    )


# --- the public-repo guard ----------------------------------------------------
#
# ``BBTT-01/ST-exporter`` is PUBLIC. A fixture recorded from a real tenant and
# committed unscrubbed publishes a contractor's customer list — permanently, and
# to everyone. Two layers, and the second is the load-bearing one:
#
# 1. pattern guards for the two identifiers that are mechanically recognisable
#    anywhere in the text (phone, email); and
# 2. a PROVENANCE guard: every cell of every fixture has to trace back to a
#    literal in ``tests/st_exporter/fixtures/``. Names, addresses and prices have
#    no recognisable shape, so no regex can ever guard them — but a recorded real
#    response cannot reach a fixture without someone hand-transcribing it into the
#    synthetic source records first, which is the point at which a human reads it.

#: Any run of 7+ digits, however it is punctuated: ``2125551234``,
#: ``(212) 555-1234``, ``212-555-1234`` and ``212.555.1234`` are all one pattern.
#: The previous ``\b\d{3}[- ]\d{4}\b`` saw only the last seven digits of the
#: first, and matched nothing at all in it.
_PHONE = re.compile(r"\+?\d[\d ().-]{5,}\d")
#: ISO timestamps are stripped before the scan: ``2026-09-03`` is eight digits
#: with separators and is not a phone number.
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)?")
#: So are the sha256 digests in manifest.json and published.json — a hex digest
#: contains long digit runs and no customer ever had one for a phone number.
_DIGEST = re.compile(r"\b[0-9a-f]{32,}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+")
#: Reserved for documentation and testing, and guaranteed never to resolve to a
#: real mailbox: RFC 2606 (`.example`/`example.com`) and RFC 6761 (`.invalid`).
#: Matched against the DOMAIN, anchored at the ``@`` — ``endswith`` on the whole
#: address waved through ``jane@notexample.com``.
_SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example.invalid")
#: The North American fictitious-number block, exactly: 555-0100 to 555-0199.
#: Nothing else in 555 is reserved — 555-1212 is a real directory-assistance
#: number — so the old "anything starting 555-" pass was not a rule at all.
_SAFE_PHONE = re.compile(r"^\d*55501\d\d$")


def _is_reserved_mailbox(address: str) -> bool:
    """Anchored at the ``@``: the DOMAIN must BE a reserved one, not merely end
    with the text of one. ``jane@notexample.com`` is a domain anybody can buy."""
    domain = address.rsplit("@", 1)[-1].lower().strip(".")
    return domain in _SAFE_EMAIL_DOMAINS


def _fixture_files() -> list[Path]:
    return sorted(FIXTURE_ROOT.rglob("*.json"))


def _fixture_grids() -> list[tuple[Path, dict[str, Any]]]:
    """Every committed tab fixture — the manifest and the register are not tabs."""
    grids = []
    for path in _fixture_files():
        document = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and "rows" in document and "columns" in document:
            grids.append((path, document))
    return grids


def test_no_fixture_carries_a_routable_email_address() -> None:
    """This is the mechanical half of the scrubbing rule in docs/export-contract.md;
    the human half is that real responses are scrubbed BEFORE they reach a branch."""
    offenders: list[str] = []
    for path in _fixture_files():
        for address in _EMAIL.findall(path.read_text(encoding="utf-8")):
            if not _is_reserved_mailbox(address):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {address}")
    assert not offenders, (
        "REAL-LOOKING EMAIL IN A PUBLIC FIXTURE:\n  "
        + "\n  ".join(offenders)
        + "\nThe DOMAIN (everything after the '@') must be exactly one of "
        + f"{', '.join(_SAFE_EMAIL_DOMAINS)}, which cannot route to a real mailbox. "
        + "'notexample.com' is a real domain someone can register."
    )


def test_no_fixture_carries_a_dialable_phone_number() -> None:
    offenders: list[str] = []
    for path in _fixture_files():
        text = _DIGEST.sub(" ", _TIMESTAMP.sub(" ", path.read_text(encoding="utf-8")))
        for candidate in _PHONE.findall(text):
            digits = re.sub(r"\D", "", candidate)
            if len(digits) < 7:
                continue
            if _SAFE_PHONE.match(digits):
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {candidate.strip()}")
    assert not offenders, (
        "REAL-LOOKING PHONE NUMBER IN A PUBLIC FIXTURE:\n  "
        + "\n  ".join(offenders)
        + "\nFixtures may only use the 555-01xx fictitious block (555-0100..555-0199), "
        + "which is the only part of 555 reserved from real assignment."
    )


@pytest.mark.parametrize(
    "number",
    ["2125551234", "(212) 555-1234", "212-555-1234", "212.555.1234", "555-1212", "+1 212 555 1234"],
    ids=lambda n: n,
)
def test_the_phone_guard_recognises_a_number_however_it_is_punctuated(number: str) -> None:
    """Executed against the guard itself: every one of these went through unflagged
    before, which is the whole reason the rule changed."""
    digits = re.sub(r"\D", "", _TIMESTAMP.sub(" ", number))
    assert _PHONE.search(number), number
    assert len(digits) >= 7 and not _SAFE_PHONE.match(digits), number


@pytest.mark.parametrize("safe", ["555-0100", "555-0199", "(555) 555-0123"], ids=lambda n: n)
def test_the_phone_guard_still_passes_the_fictitious_block(safe: str) -> None:
    digits = re.sub(r"\D", "", safe)
    assert _SAFE_PHONE.match(digits), safe


@pytest.mark.parametrize(
    "address,flagged",
    [
        ("jane@example.com", False),
        ("fixture@example.invalid", False),
        ("jane@notexample.com", True),
        ("jane@example.com.co", True),
    ],
    ids=lambda v: str(v),
)
def test_the_email_guard_is_anchored_at_the_at_sign(address: str, flagged: bool) -> None:
    """`jane@notexample.com` passed the old `endswith` check on the whole address."""
    assert (not _is_reserved_mailbox(address)) is flagged, address


#: Everything a fixture cell is allowed to be built from: the literal text of the
#: synthetic source records. Read as TEXT, not imported, so a value assembled by an
#: f-string (``f"{_APPOINTMENT_DAY}T09:00:00-05:00"``) still traces to its parts.
_SOURCE_TEXT = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted((REPO_ROOT / "tests" / "st_exporter" / "fixtures").glob("*.py"))
)

#: A word worth checking: two or more letters. Single letters (the ``T`` of an ISO
#: timestamp, a state code's halves) carry no customer identity.
_WORD = re.compile(r"[A-Za-z]{2,}")
#: A number worth checking: three or more digits. Shorter runs are the minutes and
#: months of a formatted date, and every two-digit number appears in any file.
_NUMBER = re.compile(r"\d{3,}")


def _traces_to_the_source_records(cell: str) -> bool:
    """Is every part of ``cell`` present in the synthetic source records?

    Three chances, widest first, because the exporter COMPOSES cells — it joins an
    address from its parts, joins category ids with commas, formats a timestamp
    from a date constant — and a composed value is legitimate even though the whole
    string appears nowhere.
    """
    if cell in _SOURCE_TEXT:
        return True
    if all(part.strip() in _SOURCE_TEXT for part in cell.split(",") if part.strip()):
        return True
    return all(token in _SOURCE_TEXT for token in (*_WORD.findall(cell), *_NUMBER.findall(cell)))


def test_every_fixture_cell_traces_back_to_a_synthetic_source_record() -> None:
    """The structural half of the public-repo guard, and the only one that can
    cover NAMES, ADDRESSES and PRICES.

    A phone number and an email address have a shape a regex can recognise. A
    customer's name, their street, and what they were charged do not — no pattern
    will ever tell "Fixture Customer Ltd" from a real contractor's client. What CAN
    be checked is provenance: every cell the exporter writes into a fixture is
    built from ``tests/st_exporter/fixtures/*.py``, so if a cell's words and numbers
    are not in those files, a recorded real response has been pasted straight into
    the committed suite. Adding a record there is a hand transcription — the point
    at which a human reads what they are publishing.

    This replaces asserting ``contains_real_customer_data is False``, which was a
    constant the generator hardcodes: the suite asserting its own claim.
    """
    offenders: list[str] = []
    for path, document in _fixture_grids():
        for index, row in enumerate(document["rows"]):
            for column, cell in zip(document["columns"], row, strict=False):
                if not str(cell).strip():
                    continue
                if _traces_to_the_source_records(str(cell)):
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)} row {index}, {column}: {cell!r}")
    assert not offenders, (
        "UNSOURCED CELL IN A PUBLIC FIXTURE — these values do not trace back to any "
        "literal in tests/st_exporter/fixtures/:\n  "
        + "\n  ".join(offenders)
        + "\nThe fixtures are GENERATED from those synthetic records, so a cell that "
        "came from somewhere else came from a recorded response. This repo is public: "
        "add the case to tests/st_exporter/fixtures/contract_cases.py as synthetic data "
        "and regenerate. Never paste a real tenant's response into the suite."
    )


def test_the_manifests_no_real_customer_data_claim_is_the_one_the_tests_back() -> None:
    """The flag is a claim to a consumer; the test above is what makes it true."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["contains_real_customer_data"] is False

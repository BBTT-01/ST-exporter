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
    """A renamed, removed or re-ordered column fails here, by name.

    An APPENDED column does not, and that is the one difference between this and
    plain equality. Appending is additive by ``docs/export-contract.md`` —
    consumers look columns up by name and ignore the rest — so failing here
    would force the version bump that rule exists to avoid, and a bump makes
    every consumer pinned to the old version stop parsing the tab entirely.
    ``contracts.appended_columns`` is the single definition of "append" that the
    generator asks too, so the two can never disagree.
    """
    contract, tab = _TAB_CONTRACTS[tab_name]
    committed = _load(tab_name)["columns"]
    current = list(tab.columns)
    if current == committed:
        return
    if contracts.appended_columns(committed, current):
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
    # Compare at the RELEASED width. An appended column adds a cell to every row,
    # and those cells have no committed counterpart to compare against — that is
    # the acknowledged cost of not bumping (see `contracts`' module docstring).
    # Every cell the fixture DOES pin is still checked, so the failure this test
    # exists for — a released cell quietly changing meaning, blank starting to
    # mean 0 — is untouched by the truncation.
    width = len(committed["columns"])
    if [row[:width] for row in produced] == expected:
        return
    produced = [row[:width] for row in produced]

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
    """Belt and braces over the per-tab checks: nothing in the suite is stale.

    "Produces" means what the generator would WRITE, which is why the additive
    freeze is applied here exactly as ``main`` applies it. A released fixture
    whose only difference is an appended column is deliberately left at its
    released bytes, so comparing against the raw ``build_files`` payload would
    report the suite as stale for doing precisely what it is supposed to do.
    Every non-additive difference still lands here, unchanged.
    """
    generator = _generator()
    files = generator.build_files()
    appends = generator.additive_appends(files, generator.load_register())
    if appends:
        generator.freeze_released_entries(files, appends)
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


@pytest.mark.parametrize("contract", contracts.FEEDS, ids=lambda c: c.feed)
def test_a_live_versions_registered_files_are_exactly_the_tabs_the_code_declares(
    contract: contracts.FeedContract,
) -> None:
    """A tab REMOVED from a released version is as breaking as one renamed.

    The byte-identity test above only looks at files that are both registered and
    on disk, and the generator only consulted the register for files it still
    produced — so deleting a `TabContract` from a released feed left the fixture
    and its register entry sitting there, unchanged and therefore unremarked,
    while the exporter stopped writing the tab. A consumer's tests keep passing
    against a file that now describes nothing. This asserts the set both ways:
    every registered file is still produced, and every produced file is
    registered.

    Only for versions the code still WRITES. A retired version's directory stays
    forever by design and legitimately has no code behind it any more.
    """
    published = _published()
    registered = set(published.get(contract.version, {}))
    declared = {f"{tab.name}.json" for tab in contract.tabs}
    assert registered == declared, (
        f"THE '{contract.feed}' FEED NO LONGER WRITES THE TABS {contract.version} WAS "
        f"RELEASED WITH.\n"
        f"  registered as released: {sorted(registered)}\n"
        f"  declared in the code:   {sorted(declared)}\n"
        f"  no longer produced:     {sorted(registered - declared)}\n"
        f"  not registered:         {sorted(declared - registered)}\n"
        f"Removing a tab is a BREAKING change and nothing about it is loud: the "
        f"manifest simply stops mentioning it, the committed fixture stays on disk "
        f"unchanged, and every consumer pinned to {contract.version} keeps reading a "
        f"tab the exporter no longer writes — ZERO ROWS, and `_meta` still saying "
        f"{contract.version}.\n"
        f"{contracts.published_bump_hint(contract.version)}, then "
        f"{contracts.REGENERATE_COMMAND}: {contract.version} keeps its directory for "
        f"consumers still pinned to it, and the new version describes the tabs that "
        f"actually exist."
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


def _append_a_column_to_the_generated_grid(module: Any) -> None:
    """Stand in for the additive edit: `jobs.v2` grows one column at the END.

    Built by rewriting the payload rather than by string surgery, because an
    append has to touch the header AND every row — a header-only edit would be a
    malformed grid, which is a different thing and is refused.
    """
    real = module.build_files

    def patched() -> dict[str, str]:
        files = real()
        key = f"{contracts.FIXTURE_ROOT}/jobs.v2/jobs.json"
        payload = json.loads(files[key])
        payload["columns"] = list(payload["columns"]) + ["newly_appended"]
        payload["rows"] = [list(row) + ["x"] for row in payload["rows"]]
        files[key] = module.render(payload)
        return files

    module.build_files = patched


def _reorder_columns_in_the_generated_grid(module: Any) -> None:
    """Stand in for a RE-ORDER: same set of names, different order.

    The one a set-based notion of "append" would wave through, and the one a
    consumer reading by position misreads silently.
    """
    real = module.build_files

    def patched() -> dict[str, str]:
        files = real()
        key = f"{contracts.FIXTURE_ROOT}/jobs.v2/jobs.json"
        payload = json.loads(files[key])
        columns = list(payload["columns"])
        columns[0], columns[1] = columns[1], columns[0]
        payload["columns"] = columns
        payload["rows"] = [[row[1], row[0], *row[2:]] for row in (list(r) for r in payload["rows"])]
        files[key] = module.render(payload)
        return files

    module.build_files = patched


def _append_a_column_and_retype_a_released_cell(module: Any) -> None:
    """An append riding along with a changed cell in a RELEASED column.

    The combination is the one worth pinning: the append is legitimate on its
    own, so a guard that stopped at the header would let the cell change through
    beside it.
    """
    real = module.build_files

    def patched() -> dict[str, str]:
        files = real()
        key = f"{contracts.FIXTURE_ROOT}/jobs.v2/jobs.json"
        payload = json.loads(files[key])
        payload["columns"] = list(payload["columns"]) + ["newly_appended"]
        payload["rows"] = [list(row) + ["x"] for row in payload["rows"]]
        payload["rows"][0][0] = "999999"
        files[key] = module.render(payload)
        return files

    module.build_files = patched


def _rename_a_column_in_the_generated_grid(module: Any) -> None:
    """Stand in for the source-code edit itself: what `build_files()` produces for
    `jobs.v2` now spells `job_number` as `job_no`.

    Patching the generator's own output rather than `format.py` keeps the repro
    exact — this is byte-for-byte what a rename produces — while leaving the
    installed package untouched for every other test in the session.
    """
    real = module.build_files

    def patched() -> dict[str, str]:
        files = real()
        key = f"{contracts.FIXTURE_ROOT}/jobs.v2/jobs.json"
        files[key] = files[key].replace('"job_number"', '"job_no"')
        return files

    module.build_files = patched


def _retype_a_cell_in_the_generated_grid(module: Any) -> None:
    """A genuine typo fix: one cell's TEXT differs, and nothing structural does."""
    real = module.build_files

    def patched() -> dict[str, str]:
        files = real()
        key = f"{contracts.FIXTURE_ROOT}/jobs.v2/jobs.json"
        assert "Fixture Customer Ltd" in files[key]
        files[key] = files[key].replace("Fixture Customer Ltd", "Fixture Customer Limited")
        return files

    module.build_files = patched


def _drop_a_produced_tab(module: Any, relative: str) -> None:
    """Stand in for deleting a `TabContract` from `contracts.py`: the code stops
    producing that tab, and says nothing about the fixture already on disk."""
    real = module.build_files

    def patched() -> dict[str, str]:
        files = real()
        del files[f"{contracts.FIXTURE_ROOT}/{relative}"]
        return files

    module.build_files = patched


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

    def test_republish_will_not_land_a_column_rename_however_the_changelog_reads(
        self, sandbox: tuple[Any, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The CHANGELOG line never constrained anything, and now it does not have to.

        The reviewer's repro: rename `job_number` to `job_no`, append
        `- republish jobs.v2: fixed a typo` — wording the refusal message itself
        hands you — and `--republish jobs.v2` exited 0 with `job_no` in a fixture
        still stamped `jobs.v2`. The generator cannot tell a typo from a contract
        change by reading prose, so it no longer tries: it compares the released
        file with the new payload and refuses anything structural.
        """
        module, root = sandbox
        _rename_a_column_in_the_generated_grid(module)
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n- republish jobs.v2: fixed a typo\n", encoding="utf-8"
        )

        assert _run(module, "--republish", "jobs.v2") == 2

        printed = capsys.readouterr().out
        assert "THIS IS NOT A TYPO FIX" in printed
        assert "'columns' would change" in printed
        committed = (root / contracts.FIXTURE_ROOT / "jobs.v2" / "jobs.json").read_text("utf-8")
        assert '"job_no"' not in committed

    def test_republish_will_not_drop_a_tab_from_a_released_version(
        self, sandbox: tuple[Any, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        module, root = sandbox
        _drop_a_produced_tab(module, "pricebook.v1/pricebook.equipment.json")
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n- republish pricebook.v1: tidy-up\n", encoding="utf-8"
        )

        assert _run(module, "--republish", "pricebook.v1") == 2
        assert "may not add or remove a tab" in capsys.readouterr().out

    def test_republish_still_lands_a_genuine_typo_in_a_cell(
        self, sandbox: tuple[Any, Path]
    ) -> None:
        """The escape hatch has to keep working, or the next person routes around it.

        Cell TEXT only: same columns, same row_key, same grain, same row count.
        """
        module, root = sandbox
        _retype_a_cell_in_the_generated_grid(module)
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n- republish jobs.v2: expanded an abbreviated fixture name\n",
            encoding="utf-8",
        )

        assert _run(module, "--republish", "jobs.v2") == 0

        committed = (root / contracts.FIXTURE_ROOT / "jobs.v2" / "jobs.json").read_text("utf-8")
        assert "Fixture Customer Limited" in committed
        register = json.loads((root / contracts.PUBLISHED_PATH).read_text(encoding="utf-8"))
        assert (
            register["versions"]["jobs.v2"]["jobs.json"]
            == hashlib.sha256(committed.encode("utf-8")).hexdigest()
        )

    def test_a_cell_typo_is_still_refused_without_the_republish_flag(
        self, sandbox: tuple[Any, Path]
    ) -> None:
        """Structurally-typo-only is a NARROWING of `--republish`, not a new way in:
        a changed cell is still a changed released byte and still needs the flag."""
        module, _root = sandbox
        _retype_a_cell_in_the_generated_grid(module)
        assert _run(module) == 2

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

    def test_removing_a_tab_from_a_released_version_is_refused(
        self, sandbox: tuple[Any, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The asymmetry that made this an oversight rather than a decision.

        ADDING a tab to a released version was already refused (the register has
        no sha for it). REMOVING one was not: the register was consulted only for
        files the code still PRODUCES, so a registered file the code had stopped
        producing was never a violation, the generator never deleted it, and the
        byte-identity test still found it unchanged on disk. Deleting the
        `pricebook.equipment` contract regenerated cleanly at `pricebook.v1` —
        the manifest quietly dropped the tab while TrueQuote's tests against
        `pricebook.v1` kept passing for a tab the exporter no longer wrote. Zero
        rows, no error, `_meta` still saying `pricebook.v1`.
        """
        module, root = sandbox
        _drop_a_produced_tab(module, "pricebook.v1/pricebook.equipment.json")

        assert _run(module) == 2

        printed = capsys.readouterr().out
        assert "pricebook.v1 is published — bump to pricebook.v2" in printed
        assert "the code no longer produces it" in printed
        # And the fixture it would have orphaned is still exactly where it was.
        assert (
            root / contracts.FIXTURE_ROOT / "pricebook.v1" / "pricebook.equipment.json"
        ).exists()

    def test_removing_a_tab_is_refused_in_check_mode_too(self, sandbox: tuple[Any, Path]) -> None:
        """CI runs `--check`; it must not be the lenient path for this either."""
        module, _root = sandbox
        _drop_a_produced_tab(module, "pricebook.v1/pricebook.equipment.json")
        assert _run(module, "--check") == 2

    def test_deleting_one_versions_key_from_the_register_is_refused(
        self, sandbox: tuple[Any, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The "delete the register" bypass, one level down and two lines long.

        A missing FILE entry was already a violation; a missing VERSION entry was
        read as "brand new, nothing published under it yet" — even with that
        version's directory sitting on disk. So deleting `"jobs.v2"` from the
        register and regenerating re-baselined jobs.v2 to whatever the code
        produced that day, renamed column included, and left the manifest still
        saying `jobs.v2`. A directory that already exists is not brand new.
        """
        module, root = sandbox
        path = root / contracts.PUBLISHED_PATH
        document = json.loads(path.read_text(encoding="utf-8"))
        del document["versions"]["jobs.v2"]
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

        assert _run(module) == 2
        assert "not listed in" in capsys.readouterr().out

    def test_a_deleted_version_key_is_refused_even_with_a_column_rename_riding_along(
        self, sandbox: tuple[Any, Path]
    ) -> None:
        """The reviewer's repro end to end: rename a column, delete only that
        version's key, regenerate. It used to exit 0 with `job_no` in a fixture
        still stamped `jobs.v2`."""
        module, root = sandbox
        _rename_a_column_in_the_generated_grid(module)
        path = root / contracts.PUBLISHED_PATH
        document = json.loads(path.read_text(encoding="utf-8"))
        del document["versions"]["jobs.v2"]
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

        assert _run(module) == 2
        committed = (root / contracts.FIXTURE_ROOT / "jobs.v2" / "jobs.json").read_text("utf-8")
        assert '"job_no"' not in committed
        assert '"job_number"' in committed

    # --- the additive case ----------------------------------------------------
    #
    # Appending a column is the one change `docs/export-contract.md` calls
    # additive and tells you NOT to bump for. The generator used to refuse it
    # anyway — it refused every byte change alike — so the only route it offered
    # was the bump the rule forbids, and that bump is not a harmless over-signal:
    # a consumer pinned to the old version must answer `unsupported_contract` and
    # stop parsing, so bumping for an appended column takes the tab from "missing
    # one cell" to "dark" for every consumer until each widens and deploys.
    #
    # What must NOT move with it: the released file's bytes, its sha in the
    # register, its row_count in the manifest. So these pin both halves — the
    # append is accepted, and accepting it changes nothing on disk.

    def test_an_appended_column_is_accepted_without_a_version_bump(
        self, sandbox: tuple[Any, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        module, root = sandbox
        _append_a_column_to_the_generated_grid(module)

        assert _run(module) == 0
        assert "newly_appended" in capsys.readouterr().out

    def test_an_appended_column_leaves_the_released_fixture_byte_identical(
        self, sandbox: tuple[Any, Path]
    ) -> None:
        """The whole safety of not bumping. A consumer pinned to `jobs.v2` tests
        itself against these exact bytes, so they must survive the append."""
        module, root = sandbox
        fixture = root / contracts.FIXTURE_ROOT / "jobs.v2" / "jobs.json"
        before = fixture.read_bytes()
        _append_a_column_to_the_generated_grid(module)

        assert _run(module) == 0
        assert fixture.read_bytes() == before

    def test_an_appended_column_moves_neither_the_register_nor_the_manifest(
        self, sandbox: tuple[Any, Path]
    ) -> None:
        """`published.json` is the trust anchor and is judged against the BASE
        branch, so an append that moved a recorded sha would fail
        `check_register_append_only.py` even though nothing breaking happened.
        The manifest carries the same sha and must stay describing the file that
        is really on disk."""
        module, root = sandbox
        register = root / contracts.PUBLISHED_PATH
        manifest = root / contracts.MANIFEST_PATH
        register_before = register.read_text(encoding="utf-8")
        manifest_before = manifest.read_text(encoding="utf-8")
        _append_a_column_to_the_generated_grid(module)

        assert _run(module) == 0
        assert register.read_text(encoding="utf-8") == register_before
        assert manifest.read_text(encoding="utf-8") == manifest_before

    def test_check_mode_is_clean_for_an_appended_column(self, sandbox: tuple[Any, Path]) -> None:
        """CI runs --check. An append must not report the suite as STALE: there
        is nothing to regenerate, which is the point."""
        module, _ = sandbox
        _append_a_column_to_the_generated_grid(module)

        assert _run(module, "--check") == 0

    def test_a_reordered_column_is_still_refused(self, sandbox: tuple[Any, Path]) -> None:
        """Same names, different order — what a SET-based notion of "append"
        would wave through, and what a consumer reading by position misreads
        silently. `contracts.appended_columns` is positional for this reason."""
        module, _ = sandbox
        _reorder_columns_in_the_generated_grid(module)

        assert _run(module) == 2

    def test_an_append_may_not_carry_a_changed_released_cell(
        self, sandbox: tuple[Any, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The append is legitimate on its own, so a guard that stopped at the
        header would let the cell change ride in beside it. 'Blank started
        meaning 0' is the failure the row-level check exists for, and an append
        must not become the way it lands."""
        module, _ = sandbox
        _append_a_column_and_retype_a_released_cell(module)

        assert _run(module) == 2
        assert "jobs.v2" in capsys.readouterr().out

    def test_an_append_is_not_judged_against_a_tampered_released_file(
        self, sandbox: tuple[Any, Path]
    ) -> None:
        """The released file is read from DISK, so the register's sha has to be
        checked against it first. Without that, editing the register turned any
        rewrite into "additive, nothing to see here" — laundering the exact
        bypass `published.json` exists to prevent."""
        module, root = sandbox
        _append_a_column_to_the_generated_grid(module)
        _tamper_with_the_released_jobs_sha(root)

        assert _run(module) == 2

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
#: A number worth checking: two or more digits. It was three, which quietly
#: exempted every price under $1000 with cents — ``89.95`` tokenises to ``89`` and
#: ``95``, neither of which is three digits long, so the cell carried no tokens at
#: all (see ``_traces_to_the_source_records``). Two-digit runs are noisier, but a
#: noisy guard that asks a human to look is the failure mode we want.
_NUMBER = re.compile(r"\d{2,}")


def _traces_to_the_source_records(cell: str) -> bool:
    """Is every part of ``cell`` present in the synthetic source records?

    Three chances, widest first, because the exporter COMPOSES cells — it joins an
    address from its parts, joins category ids with commas, formats a timestamp
    from a date constant — and a composed value is legitimate even though the whole
    string appears nowhere.

    **The last chance requires at least one token.** ``all([])`` is ``True``, so
    while this ended in a bare ``all(...)`` a cell that produced no tokens passed
    unconditionally: ``89.95``, ``0.99``, ``42``, ``X9`` and ``q`` were all waved
    through — i.e. every price the pricebook is most likely to carry was exempt
    from the one guard that claims to cover prices. A cell with nothing checkable
    in it has not been checked, so it falls through to the offender list and a
    human decides.

    Two honest limits, because a guard nobody knows the edges of gets trusted
    past them (``test_the_provenance_guards_documented_limits_are_real`` pins
    both):

    - **short numbers.** Tokens are matched one at a time, because the exporter
      FORMATS numbers — ``1234.0`` in a source record is ``1234.00`` in a cell —
      so requiring the whole string would flag every legitimate price. The cost
      is that a short value whose digit runs all appear somewhere in the source
      text (``12.50`` → ``12``, ``50``) traces without anyone having transcribed
      it.
    - **common-word prose.** The haystack is the TEXT of
      ``tests/st_exporter/fixtures/*.py``, comments and all, so common English
      words are in it and a memo built only from them can pass.

    Neither weakens what this is FOR: a pasted response carries names, streets,
    emails and ids, and none of those are common words or two-digit runs.
    """
    if cell in _SOURCE_TEXT:
        return True
    if all(part.strip() in _SOURCE_TEXT for part in cell.split(",") if part.strip()):
        return True
    tokens = [*_WORD.findall(cell), *_NUMBER.findall(cell)]
    return bool(tokens) and all(token in _SOURCE_TEXT for token in tokens)


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


@pytest.mark.parametrize(
    "cell",
    ["89.95", "874.19", "87.31", "0.77", "Z7", "X9"],
    ids=lambda v: str(v),
)
def test_a_cell_with_nothing_checkable_in_it_does_not_pass_the_provenance_guard(
    cell: str,
) -> None:
    """`all([])` is `True`, and that is how prices got exempted from the price guard.

    The third fallback is `all(...)` over a cell's word and number tokens. With
    numbers matched at three digits or more, a cell like `89.95` or `X9` yielded
    an EMPTY token list — and an empty `all()` is vacuously true, so it passed
    unconditionally. That is exactly the class of value the guard's own docstring
    claims to be the only cover for: a price has no recognisable shape, so no
    regex can ever guard it and provenance is all there is.

    Now a cell has to produce at least one token, and every token has to be in the
    synthetic source records. None of these strings is.
    """
    assert cell not in _SOURCE_TEXT, f"{cell!r} is in the fixtures; pick another literal"
    assert not _traces_to_the_source_records(cell)


@pytest.mark.parametrize(
    "cell",
    ["Fixture Customer Ltd", "1 Main St, Springfield, IL, 62701", "2026-09-03T09:00:00-05:00"],
    ids=lambda v: str(v),
)
def test_the_tightened_guard_still_passes_legitimate_composed_cells(cell: str) -> None:
    """A guard that cries wolf on every run is a guard somebody deletes.

    These are the three composition shapes the fallbacks exist for: a literal, an
    address joined from its parts, and a timestamp formatted from a date constant.
    """
    assert _traces_to_the_source_records(cell)


def test_the_provenance_guards_documented_limits_are_real() -> None:
    """Two limits, stated rather than implied, so nobody mistakes this for a filter.

    1. **Short numbers.** Tokens are matched individually, because the exporter
       FORMATS numbers (`1234.0` in a source record becomes `1234.00` in a cell),
       so a whole-string match would flag every legitimate price. The cost is that
       a short value whose two- and three-digit runs all happen to appear
       somewhere in the source text passes without having been transcribed.
    2. **Common-word prose.** The haystack is the TEXT of
       `tests/st_exporter/fixtures/*.py`, comments included, so common English
       words are in it and a free-text memo built only from them can trace.

    Neither weakens what the guard is FOR: a pasted response carries names,
    streets, emails and ids, and none of those are common words or short numbers.
    """
    assert _traces_to_the_source_records("12.50"), "limit 1: short numeric cells can trace"
    common = (
        "the and for with that this from have been will not but out one all any can has had "
        "job item name date time code type list new old set get run line unit part call "
        "back door work crew team"
    ).split()
    assert [word for word in common if word in _SOURCE_TEXT], (
        "No common English word appears in the fixture source text at all. That would "
        "make this guard stronger than documented — update the note in "
        "`_traces_to_the_source_records` and docs/export-contract.md rather than "
        "deleting this test."
    )

"""The trust anchor: `published.json` may only grow, judged against the base branch.

Every other contract guard in this repo is judged against a file that lives in the
branch under review. `gen_contract_fixtures.py` refuses to rewrite a released
version, refuses a missing register, refuses an unregistered version directory —
and all three read *this branch's* `contracts/fixtures/published.json`. None of
them can tell a correct register from one edited to say what the branch needed.

`scripts/check_register_append_only.py` is the one check whose authority comes
from outside the branch: it compares against the copy on the base branch, which is
already merged and released. These tests pin the comparison itself; the CI step
supplies the base ref.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _checker() -> Any:
    path = REPO_ROOT / "scripts" / "check_register_append_only.py"
    spec = importlib.util.spec_from_file_location("check_register_append_only", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BASE = {
    "jobs.v2": {"jobs.json": "a" * 64},
    "pricebook.v1": {"pricebook.services.json": "b" * 64, "pricebook.equipment.json": "c" * 64},
}


def test_an_unchanged_register_is_clean() -> None:
    assert _checker().append_only_violations(_BASE, dict(_BASE)) == []


def test_adding_a_new_version_is_allowed() -> None:
    """The whole point of the mechanism is that a new version is how you change a
    tab, so the anchor must never stand in the way of one."""
    head = {**_BASE, "jobs.v3": {"jobs.json": "d" * 64}}
    assert _checker().append_only_violations(_BASE, head) == []


def test_adding_a_file_to_a_version_the_base_does_not_have_is_allowed() -> None:
    head = {**_BASE, "jobs.v3": {"jobs.json": "d" * 64, "jobs.extra.json": "e" * 64}}
    assert _checker().append_only_violations(_BASE, head) == []


def test_a_changed_sha_under_a_published_version_is_a_violation() -> None:
    """The hand edit nothing inside the branch can catch: rename a column,
    regenerate, then edit the register to match so every in-branch check agrees."""
    head = {**_BASE, "jobs.v2": {"jobs.json": "f" * 64}}
    violations = _checker().append_only_violations(_BASE, head)
    assert len(violations) == 1
    assert "jobs.v2/jobs.json" in violations[0]


def test_a_deleted_file_entry_is_a_violation() -> None:
    head = {**_BASE, "pricebook.v1": {"pricebook.services.json": "b" * 64}}
    violations = _checker().append_only_violations(_BASE, head)
    assert len(violations) == 1
    assert "pricebook.v1/pricebook.equipment.json" in violations[0]
    assert "GONE" in violations[0]


def test_a_deleted_version_entry_is_a_violation() -> None:
    """The bypass the generator now refuses, caught from the other side too: the
    generator only sees this branch, so an edit that removes the key AND the
    directory would look consistent to it."""
    head = {"pricebook.v1": _BASE["pricebook.v1"]}
    violations = _checker().append_only_violations(_BASE, head)
    assert len(violations) == 1
    assert "jobs.v2" in violations[0]
    assert "re-baselines" in violations[0]


def test_every_published_file_is_reported_not_just_the_first() -> None:
    """A refusal that names one file invites fixing that one and re-running."""
    head = {"jobs.v2": {"jobs.json": "f" * 64}, "pricebook.v1": {}}
    assert len(_checker().append_only_violations(_BASE, head)) == 3


def test_an_unresolvable_base_ref_fails_rather_than_passing_quietly() -> None:
    """A check that could not run is not a check that passed. CI fetches the base
    branch before this step; if that fetch is ever removed, this must go red."""
    with pytest.raises(SystemExit):
        _checker()._base_register("definitely-not-a-ref-in-this-repo")


def test_it_runs_green_against_this_repos_own_first_commit_of_the_register() -> None:
    """End to end, against real git history rather than dictionaries.

    The register landed in `e119ce5`. This branch must be a superset of it — if it
    is not, either a released fixture changed in place or this test is the thing
    telling you so.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "e119ce5^{commit}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("the commit that introduced the register is not in this checkout")
    run = subprocess.run(
        ["python", str(REPO_ROOT / "scripts" / "check_register_append_only.py"), "e119ce5"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr

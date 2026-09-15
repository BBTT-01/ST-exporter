#!/usr/bin/env python
"""The trust anchor: ``published.json`` may only ever GROW, judged against the base branch.

    python scripts/check_register_append_only.py origin/main

Everything else in this repo judges the register against itself — and the
register is a file in the branch under review, so every guard built on it has the
same floor: a hand edit. `scripts/gen_contract_fixtures.py` refuses to rewrite a
released version, refuses when the register is missing, and refuses when a
version directory has no entry; all three are checks the branch's own copy of the
register has to be right for. Nothing inside the branch can tell a correct
register from one that was edited to say what the branch needed it to say.

This can, because it does not read the branch's register alone. It reads the one
on the BASE branch — code that is already merged, reviewed and released — and
requires the register under review to be a superset of it: every
``(version, file)`` that existed still exists, with the same sha256. New versions
and new files may be added; nothing already published may change or vanish.

That makes the base branch the authority, which is the only thing that can stop a
hand edit, and it is why this is a separate step rather than another assertion in
the test suite: a test in the branch is judged by the branch.

Exit codes: 0 clean, 1 a published sha changed or disappeared, 2 the check could
not be run (bad ref, unreadable JSON) — never a quiet pass.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from st_exporter import contracts  # noqa: E402  (sys.path is set up just above)


def append_only_violations(
    base: dict[str, dict[str, str]], head: dict[str, dict[str, str]]
) -> list[str]:
    """Every published ``(version, file)`` on ``base`` that ``head`` lost or changed."""
    violations: list[str] = []
    for version, files in sorted(base.items()):
        current = head.get(version)
        if current is None:
            violations.append(
                f"{version} is published on the base branch but its entry is GONE from "
                f"{contracts.PUBLISHED_PATH} — deleting it re-baselines that version on "
                f"the next regeneration"
            )
            continue
        for name, sha in sorted(files.items()):
            now = current.get(name)
            if now is None:
                violations.append(f"{version}/{name} is published on the base branch but GONE")
            elif now != sha:
                violations.append(
                    f"{version}/{name} was published as {sha[:12]}… and this branch records "
                    f"{now[:12]}… — a released fixture's recorded sha may never change"
                )
    return violations


def _versions(text: str, source: str) -> dict[str, dict[str, str]]:
    try:
        document = json.loads(text)
        versions: dict[str, dict[str, str]] = document["versions"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SystemExit(f"could not read the published register from {source}: {exc}") from exc
    return versions


def _base_register(ref: str) -> dict[str, dict[str, str]] | None:
    """The register as of ``ref``, or None if it did not exist there yet."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{contracts.PUBLISHED_PATH}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "does not exist" in stderr or "exists on disk, but not in" in stderr:
            return None  # the register post-dates this base: nothing to anchor to
        raise SystemExit(
            f"could not read {contracts.PUBLISHED_PATH} at {ref!r}: {stderr}\n"
            f"CI must fetch the base branch before this step; a ref it cannot resolve "
            f"is a check that did not run, not a check that passed."
        )
    return _versions(result.stdout, f"{ref}:{contracts.PUBLISHED_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_ref", help="the ref to anchor against, e.g. origin/main")
    args = parser.parse_args()

    base = _base_register(args.base_ref)
    if base is None:
        print(
            f"{contracts.PUBLISHED_PATH} does not exist at {args.base_ref} — nothing "
            f"published to anchor against yet."
        )
        return 0

    head_path = REPO_ROOT / contracts.PUBLISHED_PATH
    if not head_path.exists():
        print(
            f"THE PUBLISHED REGISTER IS GONE — {contracts.PUBLISHED_PATH} exists at "
            f"{args.base_ref} and not on this branch. It is the only record of the bytes "
            f"each contract version was released with."
        )
        return 1
    head = _versions(head_path.read_text(encoding="utf-8"), contracts.PUBLISHED_PATH)

    violations = append_only_violations(base, head)
    if not violations:
        print(
            f"{contracts.PUBLISHED_PATH} is append-only against {args.base_ref}: "
            f"{sum(len(files) for files in base.values())} published file(s) unchanged."
        )
        return 0

    print(f"PUBLISHED REGISTER EDITED — against {args.base_ref}:")
    for line in violations:
        print(f"      {line}")
    print(
        "\nThis file records the bytes each contract version was RELEASED with. Every "
        "other guard in this repo is judged against it, so editing it is how all of "
        "them are bypassed at once — and the branch cannot audit its own copy, which "
        "is why this step reads the base branch instead.\n"
        "TradeRated, TrueQuote and Profit Wizard each keep their own reader and each "
        "tests itself against exactly these files. A released tab that changes under "
        "an unchanged version stamp gives every one of them ZERO ROWS and no error.\n"
        f"A released version is never rewritten: bump it in src/st_exporter/contracts.py "
        f"and run {contracts.REGENERATE_COMMAND}. Restore the register with:\n"
        f"  git checkout {args.base_ref} -- {contracts.PUBLISHED_PATH}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

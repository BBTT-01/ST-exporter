"""Guards on the reusable workflow that the Python tests cannot reach.

The double-drain trap was never a Python bug — it was a YAML one, contained only
by a comment, and that comment was stripped from a live connector repo. These
assertions are the part of the guarantee that lives in `export.yml`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "export.yml"


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text()


@pytest.mark.parametrize(
    "secret",
    [
        "TRADERATED_MACHINE_TOKEN",
        "TRADERATED_OUTBOX_BASE_URL",
        "TRUEQUOTE_MACHINE_TOKEN",
        "TRUEQUOTE_OUTBOX_URL",
        "PROFITWIZARD_MACHINE_TOKEN",
        "PROFITWIZARD_OUTBOX_URL",
    ],
)
def test_every_lane_secret_is_declared_and_forwarded(workflow_text: str, secret: str) -> None:
    """Declared in `secrets:` AND passed into the run step's `env:`.

    Declaring one without forwarding it is a silent no-op — the lane would
    simply never be configured, on green runs, with no error anywhere.
    """
    assert f"      {secret}:" in workflow_text, f"{secret} is not declared"
    assert f"{secret}: ${{{{ secrets.{secret} }}}}" in workflow_text, f"{secret} is not forwarded"


def test_every_lane_secret_is_optional(workflow_text: str) -> None:
    """A contractor who bought one product must not have to invent the other
    two products' secrets to run at all."""
    secrets_block = workflow_text.split("jobs:", 1)[0]
    for line in secrets_block.splitlines():
        stripped = line.strip().rstrip(":")
        if stripped.endswith(("_MACHINE_TOKEN", "_OUTBOX_URL", "_OUTBOX_BASE_URL", "_IMAGE_TOKEN")):
            index = secrets_block.index(line)
            assert "required: false" in secrets_block[index : index + 200], stripped


def test_the_feeds_validator_accepts_every_feed_the_exporter_does(workflow_text: str) -> None:
    """The workflow validates `feeds` before `st-export` does. If it rejected a
    feed, the job asking for it would fail before Python ran — that is how the
    one job allowed to drain, or the one allowed to upload images, stops."""
    assert "jobs|technicians|pricebook|financial|images|outbox)" in workflow_text


def test_the_meta_free_feeds_take_their_own_locks(workflow_text: str) -> None:
    """Three locks, split by what a run WRITES.

    A drain-only run never reads or writes `_meta` (`cli.py` does not call
    `run_export` at all for it), so it is safe beside an export — and it has to
    be, or the 5-minute drain queues behind the hourly pricebook run. An
    images-only run passes the same test for the same reason: `run_images` never
    opens the Export Store, and the only tab it rewrites is `_image_ledger` on
    the private raw-cache Sheet. Anything else falls back to the shared export
    lock, which is the safe side.
    """
    assert (
        "group: st-export-${{ github.repository }}-"
        "${{ (inputs.feeds == 'outbox' && 'outbox') || "
        "(inputs.feeds == 'images' && 'images') || 'export' }}" in workflow_text
    )
    # Never cancel a run mid-drain: that is the crash the ledger recovers from.
    # Same for an image pass: a cancelled one loses the ledger it was about to
    # flush, which is the whole failure this split exists to end.
    assert "cancel-in-progress: false" in workflow_text


def test_every_drain_still_takes_the_same_lock(workflow_text: str) -> None:
    """The split must not weaken the one-drain rule.

    The lane half of the group key is derived from `inputs.feeds` alone, so two
    jobs that both ask for `outbox` land in the SAME group and can never run at
    once — whatever the caller named them.
    """
    group_line = next(
        line for line in workflow_text.splitlines() if line.strip().startswith("group:")
    )
    assert "github.job" not in group_line, "a per-JOB key would let two drains overlap"
    assert "github.run_id" not in group_line


def test_the_drain_announces_itself(workflow_text: str) -> None:
    """Structural enforcement still has to be checkable by a human.

    A comment explaining the one-drain rule was deleted from a live connector
    repo. This notice is printed from THIS file, at a tag the connector pins, so
    a second drainer shows up twice in the Actions list rather than silently.
    """
    assert "::notice title=Outbox drain::" in workflow_text
    assert "if: ${{ contains(inputs.feeds, 'outbox') }}" in workflow_text


# ---------------------------------------------------------------------------
# The CALLER workflow — `docs/examples/connector-export.yml`.
#
# The template repo and every contractor repo hold a copy of that file, and this
# repository cannot update a contractor's copy. Keeping the canonical one here
# is what lets the per-product job count and the one-drain rule be asserted at
# all, instead of being reviewed by eye once per connector.
# ---------------------------------------------------------------------------

CALLER = Path(__file__).resolve().parents[2] / "docs" / "examples" / "connector-export.yml"

# The four feed variables this file used to carry — `JOBS_FEED`,
# `TECHNICIANS_FEED`, `PRICEBOOK_FEED`, `FINANCIAL_FEED` — are GONE, and their
# absence is asserted below. They duplicated the ServiceTitan scopes: a
# hand-maintained second copy of what the contractor's ticked boxes already say,
# which drifts in both directions (variable on + scope missing = a red run every
# hour; variable off + scope granted = a feed they paid for silently not
# running). Every feed job now runs on its schedule for everybody and
# ServiceTitan's 403 decides what exports — see `src/st_exporter/scopes.py`.
#
# `PRICEBOOK_CATEGORY_IDS` stays. It is configuration (WHICH categories) and not
# a gate (WHETHER the feed runs), and it lives in `with:`, never in an `if:`.
FEED_VARIABLES = ("JOBS_FEED", "TECHNICIANS_FEED", "PRICEBOOK_FEED", "FINANCIAL_FEED")

FEED_JOBS = ("jobs-feed", "technicians-feed", "pricebook-feed", "financial-feed")
DRAIN_JOB = "outbox-drain"
# Not in FEED_JOBS: like the drain, it writes no tab and no `_meta` row. It is
# the job that POSTs pricebook image BYTES to TrueQuote, and it is the only job
# in the file that may hold TRUEQUOTE_IMAGE_TOKEN.
IMAGES_JOB = "images-feed"


@pytest.fixture(scope="module")
def caller() -> dict[Any, Any]:
    parsed: Any = yaml.safe_load(CALLER.read_text())
    return dict(parsed)


def _triggers(caller: dict[Any, Any]) -> dict[str, Any]:
    """The `on:` block.

    PyYAML follows YAML 1.1, where the bare word `on` is the boolean True — so
    the key of a workflow's trigger block parses as `True`, not `"on"`. GitHub
    reads the same file as the string. Accept either rather than depending on
    which YAML version the reader implements.
    """
    triggers: Any = caller.get("on", caller.get(True))
    assert isinstance(triggers, dict), "the caller workflow has no `on:` block"
    return triggers


def _schedules(caller: dict[Any, Any]) -> list[str]:
    return [entry["cron"] for entry in _triggers(caller)["schedule"]]


def _evaluate(condition: str, *, schedule: str) -> bool:
    """Evaluate one job's `if:` the way GitHub would.

    A deliberately small translator rather than a general expression engine: it
    understands exactly the grammar these conditions use, and raises on anything
    else, so a condition written in some other style fails the suite loudly
    instead of being silently evaluated as something it is not.

    It has no `vars` context at all, on purpose. A job condition that reads a
    repository variable now fails this translator rather than being quietly
    evaluated — see `test_no_feed_job_is_gated_by_a_repository_variable` for why
    that is the behaviour we want.
    """
    allowed = re.fullmatch(r"[\sA-Za-z0-9_.'*/&|()=!-]+", condition.replace("\n", " "))
    assert allowed, f"unexpected syntax in condition: {condition!r}"
    expression = condition.replace("\n", " ")
    expression = expression.replace("github.event.schedule", "SCHEDULE")
    # Absent on a schedule event; GitHub yields null, which equals no literal.
    expression = expression.replace("github.event.inputs.feed", "DISPATCH")
    expression = expression.replace("&&", "and").replace("||", "or")
    assert "github." not in expression, f"unhandled context in: {condition!r}"
    assert "vars." not in expression, f"a job gated on a repository variable: {condition!r}"
    return bool(
        eval(  # noqa: S307 - fixed grammar, asserted above, over repo-owned input
            expression,
            {"__builtins__": {}},
            {"SCHEDULE": schedule, "DISPATCH": ""},
        )
    )


def _jobs_that_run(caller: dict[Any, Any], *, schedule: str) -> set[str]:
    return {name for name, job in caller["jobs"].items() if _evaluate(job["if"], schedule=schedule)}


def _jobs_ever_run(caller: dict[Any, Any]) -> set[str]:
    """Every job this connector runs at some point in a day."""
    running: set[str] = set()
    for schedule in _schedules(caller):
        running |= _jobs_that_run(caller, schedule=schedule)
    return running


def test_every_feed_job_runs_for_every_contractor(caller: dict[Any, Any]) -> None:
    """One file, no switches. Which feeds actually EXPORT is ServiceTitan's
    answer, not this file's: a scope the tenant's app was never granted comes
    back 403 and the exporter skips that feed quietly, writing no tab."""
    assert _jobs_ever_run(caller) & set(FEED_JOBS) == set(FEED_JOBS)


@pytest.mark.parametrize("variable", FEED_VARIABLES)
def test_no_feed_job_is_gated_by_a_repository_variable(
    caller: dict[Any, Any], variable: str
) -> None:
    """No job reads one, and nothing tells a contractor to set one.

    They were a second copy of what the ServiceTitan scopes already say, and the
    drift went both ways: on + no scope is a red run every hour forever; off +
    scope granted is a feed the contractor paid for silently not running, which
    is the exact silent-stop shape the one-drain rule exists to prevent.

    The header may still NAME them — it has to, because two live connectors have
    them set and need to be told they now do nothing — but only in prose. Any
    `vars.` reference or any `gh variable set` line would be the gate coming
    back.
    """
    text = CALLER.read_text()
    assert f"vars.{variable}" not in text, f"a job still reads vars.{variable}"
    assert f"variable set {variable}" not in text, f"the file still tells someone to set {variable}"


def test_no_job_condition_reads_any_repository_variable(caller: dict[Any, Any]) -> None:
    """Not just the four by name — no `if:` may consult `vars` at all."""
    for name, job in caller["jobs"].items():
        assert "vars." not in job["if"], f"{name} is gated on a repository variable"


def test_pricebook_category_ids_survives_because_it_is_not_a_gate(caller: dict[Any, Any]) -> None:
    """The one surviving `vars` NAME. It narrows WHAT the pricebook feed exports
    and which items the image pass uploads for; it never decides WHETHER either
    runs, so it lives in `with:` and a blank value means the whole catalogue.

    Two references now, one per job, and they must agree: an image pass scoped
    to different categories than the tab it illustrates would upload pictures
    for items the Sheet does not carry.
    """
    text = CALLER.read_text()
    assert text.count("vars.") == 2, "the only repository variable left is PRICEBOOK_CATEGORY_IDS"
    assert text.count("pricebook_category_ids: ${{ vars.PRICEBOOK_CATEGORY_IDS }}") == 2
    for name in ("pricebook-feed", IMAGES_JOB):
        assert "PRICEBOOK_CATEGORY_IDS" not in caller["jobs"][name]["if"]
        assert caller["jobs"][name]["with"]["pricebook_category_ids"] == (
            "${{ vars.PRICEBOOK_CATEGORY_IDS }}"
        )


def test_the_drain_runs_exactly_once_per_cycle(caller: dict[Any, Any]) -> None:
    """The whole point of the original ticket. Not "once per product", not "once
    per enabled feed" — once, on one cadence, for everybody. With the feed
    variables gone there is no longer a combination of settings to vary: every
    job in the file runs, so this is the only configuration there is."""
    for schedule in _schedules(caller):
        running = _jobs_that_run(caller, schedule=schedule)
        drains = [name for name in running if "outbox" in caller["jobs"][name]["with"]["feeds"]]
        assert len(drains) <= 1, f"{schedule} fires {len(drains)} drains"
    assert DRAIN_JOB in _jobs_ever_run(caller)


def test_exactly_one_job_in_the_whole_file_asks_for_the_outbox_feed(caller: dict[Any, Any]) -> None:
    draining = [name for name, job in caller["jobs"].items() if "outbox" in job["with"]["feeds"]]
    assert draining == [DRAIN_JOB]


def test_the_drain_asks_for_outbox_and_nothing_else(caller: dict[Any, Any]) -> None:
    """`feeds: "outbox"` exactly. Anything else puts it back on the shared
    export lock (see the reusable workflow's concurrency group) and makes it
    write `_meta`."""
    assert caller["jobs"][DRAIN_JOB]["with"]["feeds"] == "outbox"


def test_no_feed_job_is_given_a_product_machine_token(caller: dict[Any, Any]) -> None:
    """The second, independent guarantee. Even if somebody adds `outbox` to a
    feed job's feeds, a lane needs BOTH a machine token and a URL, and no feed
    job holds a token — so it would drain nothing.

    `pricebook-feed` holds TRUEQUOTE_OUTBOX_URL and the separate
    `image_upload`-scoped TRUEQUOTE_IMAGE_TOKEN. That pair uploads image bytes
    and cannot form a drain lane.
    """
    for name in (*FEED_JOBS, IMAGES_JOB):
        held = set(caller["jobs"][name].get("secrets", {}))
        assert not {secret for secret in held if secret.endswith("_MACHINE_TOKEN")}, name


def test_the_drain_forwards_every_product_lane(caller: dict[Any, Any]) -> None:
    held = set(caller["jobs"][DRAIN_JOB]["secrets"])
    for prefix in ("TRADERATED", "TRUEQUOTE", "PROFITWIZARD"):
        assert f"{prefix}_MACHINE_TOKEN" in held
        assert {f"{prefix}_OUTBOX_URL", f"{prefix}_OUTBOX_BASE_URL"} & held, prefix


def test_the_cadences_are_the_ones_the_products_were_sold_on(caller: dict[Any, Any]) -> None:
    expected = {
        "jobs-feed": "*/5 * * * *",
        "technicians-feed": "*/30 * * * *",
        DRAIN_JOB: "*/5 * * * *",
    }
    for name, schedule in expected.items():
        assert _evaluate(caller["jobs"][name]["if"], schedule=schedule)
    # Hourly and six-hourly, on offset minutes so the long feeds do not start in
    # the same minute as a */5 jobs run and queue behind it on the export lock.
    hourly, six_hourly = "7 * * * *", "37 */6 * * *"
    assert hourly in _schedules(caller) and six_hourly in _schedules(caller)
    assert _evaluate(caller["jobs"]["pricebook-feed"]["if"], schedule=hourly)
    assert _evaluate(caller["jobs"]["financial-feed"]["if"], schedule=six_hourly)


def test_no_schedule_fires_nothing(caller: dict[Any, Any]) -> None:
    """A cron line no job claims is a run that starts, does nothing and looks
    healthy — the same silent shape as a feed that stopped exporting."""
    for schedule in _schedules(caller):
        assert _jobs_that_run(caller, schedule=schedule), schedule


def test_every_caller_job_pins_the_same_exporter_tag(caller: dict[Any, Any]) -> None:
    """The template's own comment says both `uses:` lines must be bumped
    together; there are five now, and a half-bumped caller runs two different
    exporters against one Sheet."""
    tags = {job["uses"].split("@", 1)[1] for job in caller["jobs"].values()}
    assert len(tags) == 1, tags
    tag = tags.pop()
    assert tag.startswith("exporter-v"), tag
    assert tag != "main" and "branch" not in tag


def test_the_caller_pins_the_tag_this_repo_publishes(caller: dict[Any, Any]) -> None:
    """The five `uses:` lines agreeing with each other is not enough.

    They agreed perfectly at `exporter-v0.3.0` — a tag that does not exist. Tags
    stopped at 0.2.8 while `pyproject.toml` said 0.2.9, so a contractor copying
    the file whose own header calls it "the SOURCE OF TRUTH" got a workflow
    GitHub cannot resolve: every feed and the drain stop, with the only symptom
    being runs that do not happen.

    THE SOURCE OF TRUTH IS `pyproject.toml`, and it is now the only one. This
    used to check the caller against `EXPORTER_TAG` in `export.yml` as well, and
    that literal is gone — a version string under `.github/workflows/` makes the
    release unpushable, because GITHUB_TOKEN may not push a commit that touches
    a workflow file. `export.yml` resolves its own commit at run time instead,
    so `pyproject.toml`'s version and the caller's five pins are the whole set.
    """
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    version = re.search(r'^version = "(.+)"$', pyproject, re.M)
    assert version, "pyproject.toml no longer declares a version"
    expected = f"exporter-v{version.group(1)}"
    pinned = {job["uses"].split("@", 1)[1] for job in caller["jobs"].values()}
    assert pinned == {expected}, (
        f"CALLER PINS A DIFFERENT TAG — docs/examples/connector-export.yml uses "
        f"{sorted(pinned)} but this repository publishes {expected} "
        f"(pyproject.toml, version).\n"
        f"A contractor copies that file verbatim. A tag that does not exist is not a "
        f"warning: GitHub cannot resolve the workflow, so every feed and the drain "
        f"simply stop running.\n"
        f"Bump both literals together with: ./scripts/release.sh <X.Y.Z>"
    )


def test_release_sh_rewrites_the_caller_too() -> None:
    """Its comment used to say "there are exactly two" version literals while the
    caller's five `uses:` lines sat there untouched — which is how they drifted to
    a non-existent tag in the first place."""
    release = (Path(__file__).resolve().parents[2] / "scripts" / "release.sh").read_text()
    assert "docs/examples/connector-export.yml" in release, (
        "scripts/release.sh does not touch the caller workflow, so a release leaves it "
        "pinned to the previous tag and nothing bumps it."
    )
    assert "exactly three" not in release


def test_the_caller_header_table_states_the_cadence_each_job_actually_runs_on(
    caller: dict[Any, Any],
) -> None:
    """The header table is the only part of that file a contractor reads before
    copying it, and it said `financial-feed daily` for a run that moved to
    six-hourly. A wrong table is how a contractor concludes a feed is broken."""
    header = CALLER.read_text().split("jobs:", 1)[0]
    stated = dict(re.findall(r"^#   ([a-z-]+-(?:feed|drain)) +(\S+(?: \S+)?) ", header, re.M))
    assert set(stated) == set(FEED_JOBS) | {DRAIN_JOB, IMAGES_JOB}, stated
    expected_minutes = {
        "jobs-feed": "*/5",
        "technicians-feed": "*/30",
        "pricebook-feed": "hourly",
        "financial-feed": "*/6 hours",
        IMAGES_JOB: "*/2 hours",
        DRAIN_JOB: "*/5",
    }
    assert stated == expected_minutes, (
        f"THE HEADER TABLE IS STALE — it says {stated}, the crons say {expected_minutes}."
    )
    # And the claims are grounded in the crons, not in this table.
    assert _evaluate(caller["jobs"]["financial-feed"]["if"], schedule="37 */6 * * *")
    assert _evaluate(caller["jobs"][IMAGES_JOB]["if"], schedule="22 */2 * * *")


# ---------------------------------------------------------------------------
# CI — `.github/workflows/ci.yml`.
#
# Until it existed, `.github/workflows/` held only the reusable `export.yml`,
# which never runs on a push to this repository. Every guard in this suite was
# therefore enforced only by someone remembering to type `pytest`.
# ---------------------------------------------------------------------------

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def ci() -> dict[Any, Any]:
    parsed: Any = yaml.safe_load(CI.read_text())
    return dict(parsed)


def test_ci_runs_the_suite_and_the_contract_check_on_pull_requests(ci: dict[Any, Any]) -> None:
    triggers = _triggers(ci)
    assert "pull_request" in triggers, (
        "CI must run on pull requests: a producer-side contract guard that only runs "
        "when someone remembers to type `pytest` is a theoretical guard."
    )
    steps = [step.get("run", "") for job in ci["jobs"].values() for step in job["steps"]]
    assert any(re.search(r"\bpytest\b", run) for run in steps), steps
    assert any("gen_contract_fixtures.py --check" in run for run in steps), steps


def test_ci_cannot_interfere_with_the_customer_export_workflow(ci: dict[Any, Any]) -> None:
    """`export.yml` is a REUSABLE workflow contractors call against live tenants.

    CI must stay incapable of touching it: not callable, not scheduled, handed no
    secrets, and never running the exporter itself.
    """
    triggers = _triggers(ci)
    assert "workflow_call" not in triggers, "a connector repo must not be able to call CI"
    assert "schedule" not in triggers, "CI must not run on a customer's cadence"
    text = CI.read_text()
    assert "secrets." not in text, "CI is handed no tenant credentials"
    assert "st-export-${{ github.repository }}" not in text, (
        "CI must not take either of the export workflow's concurrency locks"
    )
    steps = [step.get("run", "") for job in ci["jobs"].values() for step in job["steps"]]
    assert not any("st-export" in run for run in steps), (
        "CI must never run the exporter itself — it would write to a tab or drain a queue"
    )
    used = [str(step.get("uses", "")) for job in ci["jobs"].values() for step in job["steps"]]
    assert not any("export.yml" in entry for entry in used), used


def test_the_export_workflow_is_still_reusable_only(workflow_text: str) -> None:
    """The other direction of the same separation: adding CI must not have given
    `export.yml` a trigger of its own, which would run it here against no secrets."""
    triggers = _triggers(dict(yaml.safe_load(workflow_text)))
    assert set(triggers) == {"workflow_call"}, triggers


# ---------------------------------------------------------------------------
# RELEASE — `.github/workflows/release.yml`.
#
# The release dance (bump two literals -> commit -> merge -> tag the MERGED
# commit) has been got wrong four times: v0.2.1 tagged code the bump had not
# reached, v0.2.7 failed twice, the caller file shipped pinned to a v0.3.0 that
# never existed, and v0.2.9 went stale two merges later. The workflow performs
# the whole sequence in one job, on every merge, so it can be neither
# mis-ordered nor forgotten. These assertions cover the properties of that file
# which, if they broke, would break silently.
# ---------------------------------------------------------------------------

RELEASE = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
DRIFT = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "tag-drift.yml"


@pytest.fixture(scope="module")
def release() -> dict[Any, Any]:
    parsed: Any = yaml.safe_load(RELEASE.read_text())
    return dict(parsed)


def _release_steps(release: dict[Any, Any]) -> list[dict[str, Any]]:
    return list(release["jobs"]["release"]["steps"])


def test_the_release_runs_on_every_merge_to_the_integration_branch(
    release: dict[Any, Any],
) -> None:
    """Automatic, because "a human decides when to release" is precisely how
    v0.2.9 went stale with two merged fixes nobody published."""
    triggers = _triggers(release)
    assert set(triggers) == {"push", "workflow_dispatch"}, triggers
    assert triggers["push"]["branches"] == ["feat/servicetitan-hosted"], (
        "releases are cut from the integration branch; `main` is not the release line"
    )
    assert "paths" not in triggers["push"], (
        "what is worth releasing is decided against the newest TAG, not against one "
        "push — a docs-only merge on top of an unreleased code merge must still publish "
        "the code"
    )


def test_the_trigger_branch_and_the_branch_constant_are_the_same_branch(
    release: dict[Any, Any],
) -> None:
    """`on: push: branches:` cannot be an expression, so the name is written
    twice. The workflow refuses to run when the two disagree; this catches it a
    merge earlier."""
    branch = _triggers(release)["push"]["branches"][0]
    assert release["jobs"]["release"]["env"]["INTEGRATION_BRANCH"] == branch
    assert release["jobs"]["decide"]["steps"][-1]["env"]["INTEGRATION_BRANCH"] == branch
    names = [step.get("name", "") for step in _release_steps(release)]
    assert "Refuse if this file's own branch constants disagree" in names
    # And the drift warning must be looking at the branch releases come from.
    drift: Any = yaml.safe_load(DRIFT.read_text())
    assert drift["jobs"]["drift"]["env"]["INTEGRATION_BRANCH"] == branch


def test_the_manual_path_survives_the_automatic_one(release: dict[Any, Any]) -> None:
    """An explicit version, a forced level, a release of something the rules would
    skip, and a rehearsal — none of which the automatic path can express."""
    inputs = _triggers(release)["workflow_dispatch"]["inputs"]
    assert set(inputs["bump"]["options"]) == {"auto", "patch", "minor", "major"}
    assert inputs["bump"]["default"] == "auto"
    assert inputs["version"]["type"] == "string" and inputs["version"]["default"] == ""
    assert inputs["force"]["type"] == "boolean"
    assert inputs["dry_run"]["type"] == "boolean"


def test_the_release_cannot_re_trigger_itself(release: dict[Any, Any]) -> None:
    """The job pushes to the branch it triggers on. Two guards in this file, not
    counting GitHub's own refusal to start runs for GITHUB_TOKEN pushes — which is
    the platform's behaviour and would vanish the day the push moves to a PAT."""
    decide = release["jobs"]["decide"]["steps"][-1]["run"]
    assert "[skip release]" in decide and "[no release]" in decide
    assert "github-actions" in decide and "chore: release" in decide
    # The marker is read off the SUBJECT, not the whole message: scanning the body
    # made the commit that introduced this workflow unreleasable, because its body
    # explains the marker.
    assert 'case "$subject" in' in decide and 'case "$body" in' not in decide
    commit = next(
        step["run"] for step in _release_steps(release) if step.get("name") == "Commit the bump"
    )
    assert "[skip release]" in commit, "the bump commit must mark itself un-releasable"
    message = next(line for line in commit.splitlines() if line.strip().startswith("git commit"))
    assert "[skip release]" in message
    assert "[skip ci]" not in message, (
        "skipping CI on the branch is a different, worse thing — GitHub starts no run at "
        "all for it, and a run that never started is not a failed check"
    )


def test_a_merge_that_changes_nothing_a_tenant_runs_does_not_get_a_version(
    release: dict[Any, Any],
) -> None:
    """Docs, CHANGELOG, tests, contracts and CI are not things a contractor's run
    executes. `src/`, `pyproject.toml` and the reusable workflow are."""
    decide = release["jobs"]["decide"]["steps"][-1]["run"]
    for path in ("'^src/'", "'^pyproject\\.toml$'", "'^\\.github/workflows/export\\.yml$'"):
        assert path in decide, path
    assert 'if [ -z "$runnable" ]' in decide


def test_the_release_is_serialised_and_never_cancelled(release: dict[Any, Any]) -> None:
    """Two merges a minute apart must not compute the same next version and race
    to push it; and a run cancelled between the commit and the atomic push would
    be the one state this design refuses to leave behind."""
    assert release["concurrency"]["group"] == "release-${{ github.repository }}"
    assert release["concurrency"]["cancel-in-progress"] is False


def test_the_release_token_starts_empty_and_only_the_pushing_job_can_write(
    release: dict[Any, Any],
) -> None:
    """Minimum that can push a commit and a tag, and nothing else — no issues, no
    packages, no deployments, no Actions API. The job that parses commit messages
    and computes a version stays read-only.

    There is no second scope to add, and there is no longer anything to add it
    for. The bump used to rewrite `EXPORTER_TAG:` in
    `.github/workflows/export.yml`, and GITHUB_TOKEN is refused any ref update
    touching `.github/workflows/**` — runs 35024004355 and 35025492123 died on
    exactly that — but the permission that would allow it is not one this key
    can grant. The literal was deleted instead. See
    `test_no_workflows_scope_is_invented_to_fix_the_push` and
    `test_no_version_literal_ever_returns_to_a_workflow_file`.
    """
    assert release["permissions"] == {}, "the workflow-level token must start empty"
    assert release["jobs"]["decide"]["permissions"] == {"contents": "read"}
    assert release["jobs"]["release"]["permissions"] == {"contents": "write"}


# The complete set of scopes the Actions `permissions:` key accepts. `workflows`
# is deliberately absent: it does not exist for GITHUB_TOKEN, which is the whole
# reason the release bump must not touch a workflow file in the first place.
GITHUB_TOKEN_SCOPES = frozenset(
    {
        "actions",
        "artifact-metadata",
        "attestations",
        "checks",
        "code-quality",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "models",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-events",
        "statuses",
        "vulnerability-alerts",
    }
)


@pytest.mark.parametrize("path", [WORKFLOW, CI, RELEASE, DRIFT])
def test_no_workflows_scope_is_invented_to_fix_the_push(path: Path) -> None:
    """THE FIX THAT LOOKS OBVIOUS AND DOES NOT EXIST.

    When release run 35024004355 was rejected with "refusing to allow a GitHub
    App to create or update workflow ... without `workflows` permission", the
    reading that costs a day is that the `release` job is missing
    `permissions: workflows: write`. It is not a permission the Actions
    `permissions:` key has. Writing one there is silently meaningless — the
    push is refused exactly as before — and `actionlint` rejects the file.

    Every scope named in every workflow in this repository must be a real one,
    so the next person to reach for it fails here in a second instead of in a
    release run twenty minutes long.
    """
    parsed: Any = yaml.safe_load(path.read_text())
    doc = dict(parsed)
    blocks = [doc.get("permissions")] + [
        job.get("permissions") for job in doc.get("jobs", {}).values()
    ]
    for block in blocks:
        if not isinstance(block, dict):
            continue  # `permissions: read-all` / absent / `{}`
        unknown = set(block) - GITHUB_TOKEN_SCOPES
        assert not unknown, (
            f"{path.name} asks GITHUB_TOKEN for {sorted(unknown)}, which is not a scope the "
            "`permissions:` key grants. If this is `workflows`, it cannot be granted at all, "
            "and the answer is not a stored credential either — keep version literals out of "
            "workflow files (see release.yml's header)."
        )


def test_the_release_refuses_early_when_it_cannot_publish_what_it_would_build(
    release: dict[Any, Any],
) -> None:
    """Run 35024004355 ran every refusal, bumped, installed, ran 1200 tests,
    committed and tagged — and only then found out its token could not push a
    workflow file. Nothing landed (the push is atomic), but nothing could ever
    have landed either, and none of that work was needed to know it.

    The question is asked before the first write, by reading `export.yml`
    rather than by hardcoding the answer. It passes trivially today — the file
    carries no `EXPORTER_TAG:` line at all — which is exactly the retirement the
    step was written to reach. It is kept as the regression detector: if a
    version literal ever creeps back into a workflow file, this refuses here
    rather than at the final push.
    """
    steps = _release_steps(release)
    names = [step.get("name", "") for step in steps]
    guard = "Refuse if this push needs a credential GITHUB_TOKEN cannot have"
    assert guard in names, "the release can once again spend a tag on a push it cannot make"
    step = steps[names.index(guard)]
    # Before the bump, which is the first thing that writes.
    assert names.index(guard) < names.index("Bump both version literals (scripts/release.sh)")
    # ...and therefore before the commit and the tag, which is the point.
    assert names.index(guard) < names.index("Commit the bump") < names.index("Tag that commit")
    # It reads the live file rather than asserting a remembered answer.
    assert ".github/workflows/export.yml" in step["run"]
    assert 'if [ "$current" = "$TAG" ]' in step["run"]
    # ...and it passes, rather than refuses, when there is no literal to move.
    assert 'if [ -z "$current" ]' in step["run"], (
        "with EXPORTER_TAG gone this step reads an empty string and would refuse every "
        "release — the retirement it documents has to actually be implemented"
    )
    # There is no escape hatch. This step once stood down when a
    # RELEASE_PUSH_TOKEN secret was set; a literal back in a workflow file must
    # now fail the run outright, not be waved through by a stored credential.
    assert "env" not in step, step.get("env")
    assert "secrets." not in step["run"]
    assert "exit 1" in step["run"], "the creep-back detector no longer refuses anything"
    # A rehearsal pushes nothing, so it needs no credential and must still run.
    assert step["if"] == "${{ !inputs.dry_run }}"


def test_no_credential_that_could_rewrite_a_workflow_file_returns(
    release: dict[Any, Any],
) -> None:
    """THE SEAM EXISTED, AND REMOVING IT IS THE ASSERTION.

    This file briefly carried a `RELEASE_PUSH_TOKEN` seam — the checkout fell
    back to it when set, and the refusal step above stood down when it was. It
    was never populated, and it became unnecessary the moment the `EXPORTER_TAG`
    literal left `export.yml`: the release commit touches no workflow file, so
    GITHUB_TOKEN can push it.

    It was removed rather than left unset because anything that could go in that
    slot can rewrite the reusable workflow every contractor runs UNATTENDED
    against their own live ServiceTitan tenant. An empty slot labelled for a
    privileged token is an invitation to reopen that path the next time a push
    is refused; the right answer then is to delete the literal again.

    So: the release workflow names NO secret at all. That is the blanket claim
    it started with, recoverable now that the seam is gone, and a stronger
    statement than any allow-list of "safe" secret names.
    """
    checkout = next(
        step
        for step in _release_steps(release)
        if str(step.get("uses", "")).startswith("actions/checkout")
    )
    # Plain GITHUB_TOKEN, spelled out because loop guard (3) depends on it: a
    # push made with anything else DOES start workflow runs.
    assert checkout["with"]["token"] == "${{ github.token }}"
    assert checkout["with"]["persist-credentials"] is True
    text = RELEASE.read_text()
    assert "secrets." not in text, (
        "release.yml references a repository secret again. A credential this workflow can "
        "reach is a credential that can be given permission to rewrite .github/workflows/"
        "export.yml — the exporter every contractor runs against their own live tenant. The "
        "RELEASE_PUSH_TOKEN seam was deliberately deleted; do not reopen it."
    )
    # The decision is documented where it is made, with the run that forced it.
    assert "35024004355" in text, "the run that forced this is not named anywhere"
    assert "job.workflow_sha" in text, "the condition for removing the credential is not recorded"
    # The seam's removal is recorded rather than erased, and so are the narrower
    # options that were considered and rejected, cheapest-surface first.
    assert "RELEASE_PUSH_TOKEN" in text, "the seam was deleted silently, with no record of why"
    for option in ("create-github-app-token", "deploy key", "fine-grained PAT"):
        assert option in text, f"{option!r} is no longer recorded as a rejected alternative"


def test_the_bump_can_touch_no_workflow_file_at_all(
    release: dict[Any, Any],
) -> None:
    """`.github/workflows/export.yml` is DELIBERATELY ABSENT from the allow-list.

    A release commit that touches any file under `.github/workflows/` cannot be
    pushed by GITHUB_TOKEN — that is what killed runs 35024004355 and
    35025492123 — so the bump is allowed to move exactly two files, neither of
    them a workflow. If a version literal ever creeps back into `export.yml` and
    `release.sh` starts rewriting it, this step fails before the commit instead
    of at the push.

    Not to be confused with the READ list in the `decide` job, which still names
    `export.yml` so that a change to the reusable workflow triggers a release.
    """
    steps = _release_steps(release)
    names = [step.get("name", "") for step in steps]
    guard = next(
        step for step in steps if step.get("name") == "Assert both literals moved and agree"
    )
    run = guard["run"]
    assert "unexpected" in run and "release.sh touched files it should not have" in run
    allowed = re.findall(r"-e '\^(\S+)\$'", run)
    assert allowed == [
        r"pyproject\.toml",
        r"docs/examples/connector-export\.yml",
    ], allowed
    # And it gates the commit, which gates the tag, which gates the push.
    assert names.index("Assert both literals moved and agree") < names.index("Commit the bump")
    committed = next(step for step in steps if step.get("name") == "Commit the bump")["run"]
    add_line = next(line for line in committed.splitlines() if line.strip().startswith("git add"))
    assert add_line.strip() == "git add pyproject.toml docs/examples/connector-export.yml"
    assert ".github/workflows/" not in add_line


def test_the_workflow_resolves_its_own_source_and_refuses_before_it_fetches() -> None:
    """THE GUARD IS THE WHOLE SAFETY STORY, AND IT MUST COME FIRST.

    `export.yml` checks itself out with `job.workflow_sha` /
    `job.workflow_repository` — the `job` context, which GitHub documents for a
    reusable workflow checking out its own source. Nothing in `github.*` can
    stand in for it: all of `github.*` describes the CALLER by design, which is
    how the 2026-09-08 attempt checked out this repo's `main` and ran the wrong
    exporter against a live tenant.

    An empty `ref:` does not error — `actions/checkout` silently fetches the
    default branch and runs the wrong exporter successfully, which is how v0.2.1
    shipped. So the resolution is asserted non-empty and asserted to name this
    repository BEFORE the checkout, and this test pins that order.
    """
    export: Any = yaml.safe_load(WORKFLOW.read_text())
    steps = export["jobs"]["export"]["steps"]
    names = [str(s.get("name", "")) for s in steps]
    guard_i = names.index("Self-discovery resolved")
    checkout_i = next(
        i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert guard_i < checkout_i, (
        "the self-discovery guard runs after the checkout — by then the wrong exporter "
        "has already been fetched, and an empty ref does not fail"
    )
    guard = steps[guard_i]
    assert guard["env"]["JOB_WORKFLOW_SHA"] == "${{ job.workflow_sha }}"
    assert guard["env"]["JOB_WORKFLOW_REPOSITORY"] == "${{ job.workflow_repository }}"
    assert guard["env"]["JOB_WORKFLOW_REF"] == "${{ job.workflow_ref }}"
    # It HARD-FAILS. A warning here would let the wrong exporter run.
    assert "::error" in guard["run"] and "exit 1" in guard["run"]
    assert '-z "$JOB_WORKFLOW_SHA"' in guard["run"]
    assert '-z "$JOB_WORKFLOW_REPOSITORY"' in guard["run"]
    assert "BBTT-01/ST-exporter" in guard["run"]
    # And the checkout depends on exactly what was just proved.
    checkout = steps[checkout_i]["with"]
    assert checkout["repository"] == "${{ job.workflow_repository }}"
    assert checkout["ref"] == "${{ job.workflow_sha }}"


def test_no_version_literal_ever_returns_to_a_workflow_file() -> None:
    """THE TEST THAT STOPS THE LITERAL CREEPING BACK.

    `export.yml` carried an `EXPORTER_TAG:` literal that `scripts/release.sh`
    rewrote on every bump. That made every release commit touch
    `.github/workflows/**`, and GITHUB_TOKEN — a GitHub App installation token —
    may not push such a commit. Release runs 35024004355 and 35025492123 each
    built the bump, ran the suite, committed and tagged, and were rejected at
    the final push; `0.2.11` was unreleasable until the literal was deleted.

    There is no `permissions:` scope that fixes it and no stored credential
    worth having, since anything able to rewrite this file can rewrite the
    exporter every contractor runs against their own live tenant. So the literal
    must not come back, and the thing that replaced it must stay.
    """
    text = WORKFLOW.read_text()
    assert not re.search(r"^ +EXPORTER_TAG:", text, re.M), (
        "a version literal is back in .github/workflows/export.yml. The release bump "
        "would then rewrite a workflow file, and GITHUB_TOKEN cannot push a commit that "
        "does — runs 35024004355 and 35025492123 both died on exactly that, after a full "
        "green suite. Keep the version in pyproject.toml; export.yml resolves its own "
        "commit via job.workflow_sha."
    )
    assert "job.workflow_sha" in text, (
        "export.yml no longer resolves its own commit — without job.workflow_sha the "
        "checkout has nothing to pin to, and an empty ref silently takes the default "
        "branch (how v0.2.1 shipped the wrong exporter to a live tenant)"
    )


def test_the_release_workflow_cannot_touch_a_tenant(release: dict[Any, Any]) -> None:
    """`export.yml` is a reusable workflow running against live tenants, and
    `workflow_call` here would let a connector repository cut a tag in this one."""
    assert "workflow_call" not in _triggers(release)
    assert "pull_request" not in _triggers(release), "a fork's PR must not reach a write token"
    text = RELEASE.read_text()
    # It reads NO secret at all. The `RELEASE_PUSH_TOKEN` seam briefly made this
    # weaker — it had to be spelled out as an allow-list of one — and deleting
    # the seam restored the blanket claim, which is the one worth holding: a
    # release workflow that can reach no secret cannot leak a tenant's.
    referenced = set(re.findall(r"secrets\.([A-Z_][A-Z0-9_]*)", text))
    assert referenced == set(), referenced
    tenant_secrets = set(yaml.safe_load(WORKFLOW.read_text())[True]["workflow_call"]["secrets"])
    assert not (referenced & tenant_secrets), (
        "the release workflow names a secret export.yml hands to a live tenant"
    )
    assert "GOOGLE" not in text and "ST_CLIENT" not in text and "MACHINE_TOKEN" not in text
    steps = [step.get("run", "") for job in release["jobs"].values() for step in job["steps"]]
    assert not any(re.search(r"\bst-export\b", run) for run in steps)
    assert "st-export-${{ github.repository }}" not in text, (
        "the release workflow must not take either of the export workflow's locks"
    )
    used = [str(step.get("uses", "")) for job in release["jobs"].values() for step in job["steps"]]
    assert not any("export.yml" in entry for entry in used), used


def test_a_fork_cannot_publish_a_tag(release: dict[Any, Any]) -> None:
    """A fork's own integration branch would fire the same push trigger."""
    assert release["jobs"]["release"]["env"]["RELEASE_REPOSITORY"] == "BBTT-01/ST-exporter"
    names = [step.get("name", "") for step in _release_steps(release)]
    assert names[0] == "Refuse to release from anywhere but this repository", (
        "the repository check must come before anything else runs"
    )


def test_the_release_workflow_calls_the_script_rather_than_reimplementing_it(
    release: dict[Any, Any],
) -> None:
    """One definition of "bump the version". `scripts/release.sh` stays usable by
    hand, and the sed logic that knows where the two literals live is not copied
    into YAML, where it would drift from the script."""
    steps = [step.get("run", "") for step in _release_steps(release)]
    assert any("./scripts/release.sh" in run for run in steps), steps
    assert not any('sed "s/^      EXPORTER_TAG' in run for run in steps), (
        "the release workflow is rewriting a literal itself instead of calling the script"
    )


def test_the_release_refuses_the_ways_this_has_gone_wrong(release: dict[Any, Any]) -> None:
    """Wrong repository, wrong branch, half-repointed constants, a tree that is
    not what it expects, an existing tag. Each is a step that exits non-zero,
    checked by name so that deleting one fails the suite rather than quietly
    weakening the release."""
    names = [step.get("name", "") for step in _release_steps(release)]
    for fragment in (
        "Refuse to release from anywhere but this repository",
        "Refuse to release from anything but the integration branch",
        "Refuse if this file's own branch constants disagree",
        "Refuse unless the tree is clean and contains the triggering commit",
        "Refuse to overwrite an existing tag",
    ):
        assert fragment in names, f"{fragment!r} is gone from the release workflow"
    # And every one of them runs before the first thing that writes.
    bumped = names.index("Bump both version literals (scripts/release.sh)")
    assert names.index("Refuse to overwrite an existing tag") < bumped


def test_the_release_is_tested_before_it_is_tagged(release: dict[Any, Any]) -> None:
    """A broken release must not get a tag, and the suite must run against the
    BUMPED tree — the literal-agreement tests above are part of what is being
    verified, and before the bump they would be testing the previous release."""
    steps = _release_steps(release)
    names = [step.get("name", "") for step in steps]
    runs = [step.get("run", "") for step in steps]
    bumped = next(i for i, run in enumerate(runs) if "./scripts/release.sh" in run)
    tested = next(i for i, run in enumerate(runs) if re.search(r"^\s*pytest\b", run, re.M))
    committed = names.index("Commit the bump")
    tagged = names.index("Tag that commit")
    pushed = next(i for i, run in enumerate(runs) if "git push" in run)
    assert bumped < tested < committed < tagged < pushed, names


def test_the_tag_is_verified_against_its_own_commit_before_anything_is_pushed(
    release: dict[Any, Any],
) -> None:
    """THE TAGGED COMMIT MUST AGREE WITH ITSELF.

    `pyproject.toml` and the example caller are read back out of the TAGGED TREE
    (`git show $TAG:...`), not out of the working tree, before the push.

    `export.yml` is no longer one of the things compared — it carries no version
    literal — but it is still read here, for the opposite reason: to prove the
    literal did NOT come back. `EXPORTER_TAG` appearing in the tagged workflow
    file is what made runs 35024004355 and 35025492123 unpushable.
    """
    steps = _release_steps(release)
    names = [step.get("name", "") for step in steps]
    proof = names.index("Prove the tagged commit agrees with itself")
    pushed = next(i for i, step in enumerate(steps) if "git push" in step.get("run", ""))
    assert proof < pushed
    run = steps[proof]["run"]
    assert 'git show "$TAG:docs/examples/connector-export.yml"' in run
    assert 'git show "$TAG:pyproject.toml"' in run
    # Present as the ABSENCE guard, not as a cross-check.
    assert "EXPORTER_TAG" in run, (
        "nothing stops a version literal returning to export.yml in the tagged tree"
    )


def test_the_tag_is_cut_from_the_commit_by_sha_not_by_head(release: dict[Any, Any]) -> None:
    tag_step = next(
        step for step in _release_steps(release) if step.get("name") == "Tag that commit"
    )
    assert 'git tag -a "$TAG" "$SHA"' in tag_step["run"]


def test_the_commit_and_the_tag_are_pushed_atomically(release: dict[Any, Any]) -> None:
    """A half-push is one of the shapes being eliminated: a tag whose commit the
    branch does not have, or a bump commit nobody tagged. It is also what makes a
    branch that moved under the run reject the whole release rather than half."""
    push = next(
        step["run"] for step in _release_steps(release) if "git push" in step.get("run", "")
    )
    assert "--atomic" in push
    assert "refs/tags/$TAG" in push
    assert "HEAD:refs/heads/$INTEGRATION_BRANCH" in push


def test_the_drift_warning_never_fails_a_build() -> None:
    """Information, not a gate: a branch legitimately sits ahead of the newest tag
    for the minutes a release run takes, and for any merge the rules skip."""
    drift: Any = yaml.safe_load(DRIFT.read_text())
    assert set(_triggers(dict(drift))) == {"schedule", "workflow_dispatch"}
    assert drift["permissions"] == {"contents": "read"}
    step = drift["jobs"]["drift"]["steps"][-1]
    assert step["continue-on-error"] is True
    assert step["run"].rstrip().endswith("exit 0")
    assert "::warning" in step["run"] and "::error" not in step["run"]


# ---------------------------------------------------------------------------
# The `images` feed — its own job, its own lock, its own timeout, and the only
# job in the file holding TRUEQUOTE_IMAGE_TOKEN.
#
# It used to be a flag on `pricebook-feed`. Run 35130164187 on
# `BBTT-01/tr-doorservpro` was SIGKILLed at 10m35s having uploaded 0 of ~7,191
# images, and took the hourly pricebook export down with it every hour. Each
# assertion below is one of the three things that had to be true for that to
# stop happening.
# ---------------------------------------------------------------------------


def test_the_image_pass_is_a_job_of_its_own(caller: dict[Any, Any]) -> None:
    """`feeds: "images"` exactly, and it runs on its own schedule.

    Anything else in its `feeds:` puts it back on the shared export lock (see
    the reusable workflow's concurrency group) and makes it write `_meta`.
    """
    assert caller["jobs"][IMAGES_JOB]["with"]["feeds"] == "images"
    assert IMAGES_JOB in _jobs_ever_run(caller)


def test_no_other_job_asks_for_the_images_feed(caller: dict[Any, Any]) -> None:
    """Two image passes would re-tread the same catalogue against one ledger and
    halve the forward progress each run makes."""
    uploading = [name for name, job in caller["jobs"].items() if "images" in job["with"]["feeds"]]
    assert uploading == [IMAGES_JOB]


def test_only_the_images_job_is_given_the_image_token(caller: dict[Any, Any]) -> None:
    """The token moved off `pricebook-feed`, and it must not come back.

    A copy left there is not merely redundant: it is what used to make the
    pricebook feed download thousands of images inside its own ten-minute job,
    which is the failure this split exists to end.
    """
    holding = [
        name
        for name, job in caller["jobs"].items()
        if "TRUEQUOTE_IMAGE_TOKEN" in (job.get("secrets") or {})
    ]
    assert holding == [IMAGES_JOB]


def test_the_images_job_raises_its_own_timeout_and_nothing_else_does(
    caller: dict[Any, Any],
) -> None:
    """One job passes `job_timeout_minutes`, and it is this one.

    Every other job leaves it unset and therefore keeps the reusable workflow's
    default of 10 — which is the whole point of giving the input a default
    rather than making every caller state a number.
    """
    passing = {
        name: job["with"]["job_timeout_minutes"]
        for name, job in caller["jobs"].items()
        if "job_timeout_minutes" in (job.get("with") or {})
    }
    assert set(passing) == {IMAGES_JOB}
    assert passing[IMAGES_JOB] > 10


def test_the_reusable_workflow_defaults_the_timeout_to_ten(workflow_text: str) -> None:
    """The default is what keeps every EXISTING caller and every other feed on
    exactly the behaviour they had before this input existed."""
    inputs: Any = yaml.safe_load(workflow_text)
    declared = inputs.get("on", inputs.get(True))["workflow_call"]["inputs"]
    assert declared["job_timeout_minutes"]["default"] == 10
    assert "timeout-minutes: ${{ inputs.job_timeout_minutes }}" in workflow_text


def test_the_exporter_is_told_the_runner_deadline(workflow_text: str) -> None:
    """Forwarding it is not optional and its absence is SILENT.

    Without `EXPORTER_JOB_TIMEOUT_MINUTES` the exporter assumes the default ten
    minutes, so a job given thirty would be killed by the runner at thirty
    having stopped uploading at eight — or, worse, a job given ten would be
    killed at ten with an unflushed ledger, which is the original bug exactly.
    """
    assert "EXPORTER_JOB_TIMEOUT_MINUTES: ${{ inputs.job_timeout_minutes }}" in workflow_text


def test_the_per_run_image_asset_cap_defaults_to_unlimited(workflow_text: str) -> None:
    """0 = no cap, and that is the default on purpose.

    A number here would silently truncate a large catalogue for ever on every
    caller that never chose one, and a tenant permanently missing its last N
    images looks exactly like a clean run.
    """
    inputs: Any = yaml.safe_load(workflow_text)
    declared = inputs.get("on", inputs.get(True))["workflow_call"]["inputs"]
    assert declared["image_max_assets"]["default"] == 0


def test_the_exporter_is_told_the_asset_cap(workflow_text: str) -> None:
    """Forwarding it is not optional and its absence is SILENT: the input would
    accept a number and the run would ignore it."""
    assert "EXPORTER_IMAGE_MAX_ASSETS: ${{ inputs.image_max_assets }}" in workflow_text


def test_the_image_concurrency_dial_exists_and_defaults_to_eight(workflow_text: str) -> None:
    """The number that turns a ~20-hour first sync into a ~2.5-hour one.

    Eight rather than one because the image pass is almost entirely network
    wait, and eight rather than thirty-two because a payload can be 8 MiB and
    the bound on memory is the worker count.
    """
    inputs: Any = yaml.safe_load(workflow_text)
    declared = inputs.get("on", inputs.get(True))["workflow_call"]["inputs"]
    assert declared["image_concurrency"]["default"] == 8


def test_the_exporter_is_told_the_image_concurrency(workflow_text: str) -> None:
    """Forwarding it is not optional and its absence is SILENT: the input would
    accept a number and every run would quietly stay on the default."""
    assert "EXPORTER_IMAGE_CONCURRENCY: ${{ inputs.image_concurrency }}" in workflow_text


def test_the_rate_ceiling_is_declared_and_forwarded(workflow_text: str) -> None:
    """A concurrency dial with no governor behind it is how a pass discovers
    somebody's rate limit by collecting 429s."""
    inputs: Any = yaml.safe_load(workflow_text)
    declared = inputs.get("on", inputs.get(True))["workflow_call"]["inputs"]
    assert declared["image_requests_per_second"]["default"] == 6
    assert (
        "EXPORTER_IMAGE_REQUESTS_PER_SECOND: ${{ inputs.image_requests_per_second }}"
        in workflow_text
    )

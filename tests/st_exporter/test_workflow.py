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


def test_the_feeds_validator_accepts_the_outbox_feed(workflow_text: str) -> None:
    """The workflow validates `feeds` before `st-export` does. If it rejected
    `outbox`, the one job allowed to drain would fail before Python ran."""
    assert "jobs|technicians|pricebook|financial|outbox)" in workflow_text


def test_the_drain_and_the_export_feeds_take_different_locks(workflow_text: str) -> None:
    """Two locks, split by what a run WRITES.

    A drain-only run never reads or writes `_meta` (`cli.py` does not call
    `run_export` at all for it), so it is safe beside an export — and it has to
    be, or the 5-minute drain queues behind the hourly pricebook run. Anything
    else falls back to the shared export lock, which is the safe side.
    """
    assert (
        "group: st-export-${{ github.repository }}-"
        "${{ inputs.feeds == 'outbox' && 'outbox' || 'export' }}" in workflow_text
    )
    # Never cancel a run mid-drain: that is the crash the ledger recovers from.
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
    """The one surviving `vars` reference. It narrows WHAT the pricebook feed
    exports; it never decides WHETHER the feed runs, so it lives in `with:` and
    a blank value exports the whole catalogue."""
    text = CALLER.read_text()
    assert text.count("vars.") == 1, "the only repository variable left is PRICEBOOK_CATEGORY_IDS"
    assert "pricebook_category_ids: ${{ vars.PRICEBOOK_CATEGORY_IDS }}" in text
    assert "PRICEBOOK_CATEGORY_IDS" not in caller["jobs"]["pricebook-feed"]["if"]


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
    for name in FEED_JOBS:
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
    stop at 0.2.8 and `EXPORTER_TAG`/`pyproject.toml` say 0.2.9, so a contractor
    copying the file whose own header calls it "the SOURCE OF TRUTH" got a
    workflow GitHub cannot resolve: every feed and the drain stop, with the only
    symptom being runs that do not happen.

    So the caller is checked against the ONE literal in `export.yml`, which is
    what `scripts/release.sh` moves and what the runtime guard verifies against
    the installed package.
    """
    published = re.search(r"^ +EXPORTER_TAG: (exporter-v\S+)$", WORKFLOW.read_text(), re.M)
    assert published, "export.yml no longer declares a single EXPORTER_TAG literal"
    expected = published.group(1)
    pinned = {job["uses"].split("@", 1)[1] for job in caller["jobs"].values()}
    assert pinned == {expected}, (
        f"CALLER PINS A DIFFERENT TAG — docs/examples/connector-export.yml uses "
        f"{sorted(pinned)} but this repository publishes {expected} "
        f"(.github/workflows/export.yml, EXPORTER_TAG).\n"
        f"A contractor copies that file verbatim. A tag that does not exist is not a "
        f"warning: GitHub cannot resolve the workflow, so every feed and the drain "
        f"simply stop running.\n"
        f"Bump all three literals together with: ./scripts/release.sh <X.Y.Z>"
    )


def test_the_pinned_tag_is_the_version_in_pyproject(caller: dict[Any, Any]) -> None:
    """...and the tag names the version this source tree actually is."""
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    version = re.search(r'^version = "(.+)"$', pyproject, re.M)
    assert version
    tag = {job["uses"].split("@", 1)[1] for job in caller["jobs"].values()}.pop()
    assert tag == f"exporter-v{version.group(1)}", (
        f"{tag} does not name pyproject.toml's version {version.group(1)}. "
        f"Use ./scripts/release.sh <X.Y.Z>, which moves all three together."
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
    assert "exactly two" not in release


def test_the_caller_header_table_states_the_cadence_each_job_actually_runs_on(
    caller: dict[Any, Any],
) -> None:
    """The header table is the only part of that file a contractor reads before
    copying it, and it said `financial-feed daily` for a run that moved to
    six-hourly. A wrong table is how a contractor concludes a feed is broken."""
    header = CALLER.read_text().split("jobs:", 1)[0]
    stated = dict(re.findall(r"^#   ([a-z-]+-(?:feed|drain)) +(\S+(?: \S+)?) ", header, re.M))
    assert set(stated) == set(FEED_JOBS) | {DRAIN_JOB}, stated
    expected_minutes = {
        "jobs-feed": "*/5",
        "technicians-feed": "*/30",
        "pricebook-feed": "hourly",
        "financial-feed": "*/6 hours",
        DRAIN_JOB: "*/5",
    }
    assert stated == expected_minutes, (
        f"THE HEADER TABLE IS STALE — it says {stated}, the crons say {expected_minutes}."
    )
    # And the claim about `financial-feed` is grounded in the cron, not in this table.
    assert _evaluate(caller["jobs"]["financial-feed"]["if"], schedule="37 */6 * * *")


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

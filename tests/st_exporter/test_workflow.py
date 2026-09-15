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

# Which variables a contractor sets, per product bought. This table IS the
# ticket's "Bought with" column.
PRODUCT_VARIABLES: dict[str, tuple[str, ...]] = {
    "traderated": ("JOBS_FEED", "TECHNICIANS_FEED"),
    "truequote": ("PRICEBOOK_FEED",),
    "profitwizard": ("JOBS_FEED", "TECHNICIANS_FEED", "FINANCIAL_FEED"),
}

FEED_JOBS = ("jobs-feed", "technicians-feed", "pricebook-feed", "financial-feed")
DRAIN_JOB = "outbox-drain"

# A contractor who bought everything: used where the question is the cadence or
# the shape of a job, not which products switch it on.
_ALL_ON: dict[str, str] = {name: "true" for names in PRODUCT_VARIABLES.values() for name in names}


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


def _evaluate(condition: str, *, variables: dict[str, str], schedule: str) -> bool:
    """Evaluate one job's `if:` the way GitHub would.

    A deliberately small translator rather than a general expression engine: it
    understands exactly the grammar these conditions use, and raises on anything
    else, so a condition written in some other style fails the suite loudly
    instead of being silently evaluated as something it is not.
    """
    allowed = re.fullmatch(r"[\sA-Za-z0-9_.'*/&|()=!-]+", condition.replace("\n", " "))
    assert allowed, f"unexpected syntax in condition: {condition!r}"
    expression = condition.replace("\n", " ")
    expression = expression.replace("github.event.schedule", "SCHEDULE")
    # Absent on a schedule event; GitHub yields null, which equals no literal.
    expression = expression.replace("github.event.inputs.feed", "DISPATCH")
    expression = re.sub(r"vars\.([A-Z_]+)", r"VARS.get('\1', '')", expression)
    expression = expression.replace("&&", "and").replace("||", "or")
    assert "github." not in expression, f"unhandled context in: {condition!r}"
    return bool(
        eval(  # noqa: S307 - fixed grammar, asserted above, over repo-owned input
            expression,
            {"__builtins__": {}},
            {"VARS": variables, "SCHEDULE": schedule, "DISPATCH": ""},
        )
    )


def _jobs_that_run(caller: dict[Any, Any], *, variables: dict[str, str], schedule: str) -> set[str]:
    return {
        name
        for name, job in caller["jobs"].items()
        if _evaluate(job["if"], variables=variables, schedule=schedule)
    }


def _jobs_ever_run(caller: dict[Any, Any], *, variables: dict[str, str]) -> set[str]:
    """Every job this contractor runs at some point in a day."""
    running: set[str] = set()
    for schedule in _schedules(caller):
        running |= _jobs_that_run(caller, variables=variables, schedule=schedule)
    return running


def _variables_for(*products: str) -> dict[str, str]:
    enabled = {name for product in products for name in PRODUCT_VARIABLES[product]}
    return {name: "true" for name in enabled}


def test_a_reviews_only_contractor_runs_exactly_two_feed_jobs(caller: dict[Any, Any]) -> None:
    """TradeRated buys jobs and technicians. It does not buy the contractor's
    price book, invoices, timesheets or payroll, and must not export them."""
    running = _jobs_ever_run(caller, variables=_variables_for("traderated"))
    assert running & set(FEED_JOBS) == {"jobs-feed", "technicians-feed"}


def test_a_contractor_with_all_three_products_runs_four_feed_jobs(caller: dict[Any, Any]) -> None:
    running = _jobs_ever_run(
        caller, variables=_variables_for("traderated", "truequote", "profitwizard")
    )
    assert running & set(FEED_JOBS) == set(FEED_JOBS)


def test_a_truequote_only_contractor_exports_only_the_price_book(caller: dict[Any, Any]) -> None:
    """The reverse direction of the same rule: no jobs tab, so no customer names
    or addresses, for a contractor who bought a catalogue tool."""
    running = _jobs_ever_run(caller, variables=_variables_for("truequote"))
    assert running & set(FEED_JOBS) == {"pricebook-feed"}


def test_every_feed_job_is_off_until_a_variable_turns_it_on(caller: dict[Any, Any]) -> None:
    """A repository created from the template, with no variables set, exports
    nothing at all — and cannot start emailing a new owner about feeds they did
    not buy."""
    assert not _jobs_ever_run(caller, variables={}) & set(FEED_JOBS)


@pytest.mark.parametrize(
    "products",
    [
        (),
        ("traderated",),
        ("truequote",),
        ("profitwizard",),
        ("traderated", "truequote"),
        ("traderated", "truequote", "profitwizard"),
    ],
)
def test_the_drain_runs_exactly_once_per_cycle_whatever_was_bought(
    caller: dict[Any, Any], products: tuple[str, ...]
) -> None:
    """The whole point of the ticket. Not "once per product", not "once per
    enabled feed" — once, on one cadence, for everybody."""
    variables = _variables_for(*products)
    for schedule in _schedules(caller):
        running = _jobs_that_run(caller, variables=variables, schedule=schedule)
        drains = [name for name in running if "outbox" in caller["jobs"][name]["with"]["feeds"]]
        assert len(drains) <= 1, f"{schedule} fires {len(drains)} drains"
    assert DRAIN_JOB in _jobs_ever_run(caller, variables=variables)


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
        assert _evaluate(caller["jobs"][name]["if"], variables=_ALL_ON, schedule=schedule)
    # Hourly and daily, on offset minutes so the long feeds do not start in the
    # same minute as a */5 jobs run and queue behind it on the export lock.
    hourly, daily = "7 * * * *", "37 3 * * *"
    assert hourly in _schedules(caller) and daily in _schedules(caller)
    assert _evaluate(caller["jobs"]["pricebook-feed"]["if"], variables=_ALL_ON, schedule=hourly)
    assert _evaluate(caller["jobs"]["financial-feed"]["if"], variables=_ALL_ON, schedule=daily)


def test_no_schedule_fires_nothing(caller: dict[Any, Any]) -> None:
    """A cron line no job claims is a run that starts, does nothing and looks
    healthy — the same silent shape as a feed that stopped exporting."""
    for schedule in _schedules(caller):
        assert _jobs_that_run(caller, variables=_ALL_ON, schedule=schedule), schedule


def test_every_caller_job_pins_the_same_exporter_tag(caller: dict[Any, Any]) -> None:
    """The template's own comment says both `uses:` lines must be bumped
    together; there are five now, and a half-bumped caller runs two different
    exporters against one Sheet."""
    tags = {job["uses"].split("@", 1)[1] for job in caller["jobs"].values()}
    assert len(tags) == 1, tags
    tag = tags.pop()
    assert tag.startswith("exporter-v"), tag
    assert tag != "main" and "branch" not in tag

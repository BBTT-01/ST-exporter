"""Guards on the reusable workflow that the Python tests cannot reach.

The double-drain trap was never a Python bug — it was a YAML one, contained only
by a comment, and that comment was stripped from a live connector repo. These
assertions are the part of the guarantee that lives in `export.yml`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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

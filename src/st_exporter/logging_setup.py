"""Logging setup: st_exporter's own messages visible, transport loggers quiet.

Two separate concerns, both handled explicitly rather than by relying on root:

1. httpx/httpcore will dump full request headers — including our ``Authorization``
   bearer token and ``ST-App-Key`` — at DEBUG level if anything in the process ever
   sets those loggers (or the root logger) to DEBUG. Nothing does today, but that's
   exactly the kind of thing that gets added "temporarily to debug an issue" and
   then ships, so these are pinned to WARNING unconditionally.
2. Without its own level/handler, the ``st_exporter`` logger inherits root's
   default WARNING — meaning every ``logger.info(...)`` call in this package
   (delta counts, skip counts, the dry-run summary) is silently discarded in a
   GitHub Actions runner, which configures no logging of its own. Giving
   ``st_exporter`` its own INFO level and handler fixes that without touching
   root or weakening (1)'s security rationale.

``configure_logging`` is called unconditionally at the top of every run.

``announce_to_actions`` is the third concern: a WARNING in the log of a run that
SUCCEEDS is invisible. GitHub only surfaces a message if it is an annotation or a
step summary, so anything a human must actually see has to be emitted as both.
`export.yml` already does this by hand for the drain notice; this is the same
thing for warnings raised from Python.
"""

from __future__ import annotations

import logging
import os

_QUIET_LOGGERS = ("httpx", "httpcore")

logger = logging.getLogger("st_exporter")


def _escape(value: str) -> str:
    """GitHub's workflow-command escaping: a raw newline would end the command."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def announce_to_actions(title: str, message: str, *, level: str = "warning") -> None:
    """Also surface ``message`` in the GitHub Actions UI, if that is where we are.

    Two channels, because they answer different questions: the annotation puts a
    coloured line on the run itself (visible without opening the log of a green
    run — which is the whole problem), and the step summary is what somebody
    reading the run afterwards sees first.

    ``level`` is the Actions workflow-command name: ``warning`` for "this looks
    wrong, check it", ``error`` for "work is not getting done" — a red annotation,
    which is what a queue that is draining nothing needs. It changes the colour
    only; neither one fails the step, so the caller still owns the exit code.

    A no-op outside Actions, and it NEVER raises: every caller is a detector, and a
    detector must not be able to fail the run it is watching.
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        print(f"::{level} title={_escape(title)}::{_escape(message)}", flush=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"- **{title}** — {message}\n")
    except Exception:  # a notice must never break the run it is reporting on
        logger.debug("could not emit a GitHub Actions annotation", exc_info=True)


def _make_visible(target: logging.Logger) -> None:
    target.setLevel(logging.INFO)
    if not target.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        target.addHandler(handler)
    # This logger's own messages shouldn't also go through root's handlers (if the
    # hosting environment ever adds any) and print twice.
    target.propagate = False


def configure_logging() -> None:
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _make_visible(logger)
    # `st_cli` is a separate package with a separate logger, and (1)'s rationale
    # does not cover it: it is OUR code, not a transport library, and it says
    # only what it chose to say. It gets the same treatment because the one thing
    # it narrates — "I am parked on a 429 for the next fifty seconds" — is
    # otherwise an exporter run printing nothing for minutes at a time, which
    # reads as a hang. Silence during a throttle is what run 35159471697 on
    # `BBTT-01/tr-doorservpro` looked like: seven minutes, not one line.
    _make_visible(logging.getLogger("st_cli"))

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
"""

from __future__ import annotations

import logging

_QUIET_LOGGERS = ("httpx", "httpcore")

logger = logging.getLogger("st_exporter")


def configure_logging() -> None:
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    # This logger's own messages shouldn't also go through root's handlers (if the
    # hosting environment ever adds any) and print twice.
    logger.propagate = False

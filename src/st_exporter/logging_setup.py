"""Force noisy transport loggers quiet, regardless of ambient logging config.

httpx/httpcore will dump full request headers — including our ``Authorization``
bearer token and ``ST-App-Key`` — at DEBUG level if anything in the process ever
sets those loggers (or the root logger) to DEBUG. Nothing does today, but that's
exactly the kind of thing that gets added "temporarily to debug an issue" and then
ships. ``configure_logging`` is called unconditionally at the top of every run so
this can't happen by surprise, and deliberately does not touch the root logger's
own level or handlers — that's the hosting environment's call, not ours.
"""

from __future__ import annotations

import logging

_QUIET_LOGGERS = ("httpx", "httpcore")

logger = logging.getLogger("st_exporter")


def configure_logging() -> None:
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

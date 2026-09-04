"""``st dispatch export-appointment-assignments`` — resolves `st_technician_id`.

This is an event feed (assign/unassign over time), not a current-state table — see
``denormalize.py`` for how a single technician per appointment is derived from it.
"""

from __future__ import annotations

from typing import Any

from st_cli.client import ServiceTitanClient
from st_exporter.feeds._export import fetch_export_delta

MODULE = "dispatch"
FEED = "appointment-assignments"


def fetch_assignments_delta(
    client: ServiceTitanClient, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    return fetch_export_delta(client, MODULE, FEED, cursor)

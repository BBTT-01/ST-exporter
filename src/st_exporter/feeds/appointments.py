"""``st jobs export-appointments`` — row cardinality of the `jobs` tab comes from this."""

from __future__ import annotations

from typing import Any

from st_cli.client import ServiceTitanClient
from st_exporter.feeds._export import fetch_export_delta

MODULE = "jpm"
FEED = "appointments"


def fetch_appointments_delta(
    client: ServiceTitanClient, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    return fetch_export_delta(client, MODULE, FEED, cursor)

"""``st jobs export-jobs`` — one of the two feeds the `jobs` tab rows come from."""

from __future__ import annotations

from typing import Any

from st_cli.client import ServiceTitanClient
from st_exporter.feeds._export import fetch_export_delta

MODULE = "jpm"
FEED = "jobs"


def fetch_jobs_delta(
    client: ServiceTitanClient, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    return fetch_export_delta(client, MODULE, FEED, cursor)

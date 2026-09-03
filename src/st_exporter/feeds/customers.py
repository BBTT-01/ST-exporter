"""``st crm export-customers`` — fetched, never exported as its own tab (spec.md)."""

from __future__ import annotations

from typing import Any

from st_cli.client import ServiceTitanClient
from st_exporter.feeds._export import fetch_export_delta

MODULE = "crm"
FEED = "customers"


def fetch_customers_delta(
    client: ServiceTitanClient, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    return fetch_export_delta(client, MODULE, FEED, cursor)

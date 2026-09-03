"""Shared helper behind the five per-feed wrappers in this package."""

from __future__ import annotations

from typing import Any

from st_cli.client import ServiceTitanClient
from st_cli.pagination import fetch_export_all


def fetch_export_delta(
    client: ServiceTitanClient,
    module: str,
    feed: str,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Drain a change-feed from ``cursor`` to its current end.

    Returns ``(records, next_cursor)``. Passing ``next_cursor`` back on the next
    call fetches only what changed since this call — that's the whole incremental
    contract; everything else in this package is just naming the right module/feed.
    """
    return fetch_export_all(client, module, feed, continue_from=cursor)

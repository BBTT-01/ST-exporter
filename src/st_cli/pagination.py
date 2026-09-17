"""Pagination helpers for ServiceTitan list endpoints."""

from __future__ import annotations

from typing import Any, Iterator, cast

from st_cli.client import ServiceTitanClient


def fetch_page(
    client: ServiceTitanClient,
    module: str,
    resource: str,
    params: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Fetch a single page. Returns the full envelope (page, pageSize, hasMore, totalCount,
    data)."""
    p = dict(params or {})
    p["page"] = page
    p["pageSize"] = page_size
    return cast(dict[str, Any], client.get(module, resource, params=p))


def fetch_all(
    client: ServiceTitanClient,
    module: str,
    resource: str,
    params: dict[str, Any] | None = None,
    page_size: int = 50,
) -> Iterator[dict[str, Any]]:
    """Auto-paginate until hasMore=False, yielding each record."""
    page = 1
    while True:
        envelope = fetch_page(client, module, resource, params, page=page, page_size=page_size)
        for record in envelope.get("data", []):
            yield record
        if not envelope.get("hasMore", False):
            break
        page += 1


def fetch_export_page(
    client: ServiceTitanClient,
    module: str,
    feed: str,
    continue_from: str | None = None,
    include_recent_changes: bool = False,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch one page of an Export change-feed.

    Export feeds live at ``{module}/v2/tenant/{id}/export/{feed}`` and return
    ``{ data[], continueFrom, hasMore }``. The ``continueFrom`` token from one
    response is passed back to fetch the next page (and, once ``hasMore`` is
    false, to poll for future deltas).
    """
    p = dict(params or {})
    if continue_from is not None:
        p["from"] = continue_from
    if include_recent_changes:
        p["includeRecentChanges"] = True
    return cast(dict[str, Any], client.get(module, f"export/{feed}", params=p))


def fetch_export_all(
    client: ServiceTitanClient,
    module: str,
    feed: str,
    continue_from: str | None = None,
    params: dict[str, Any] | None = None,
    max_records: int | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Drain an Export change-feed from ``continue_from`` to the end.

    Returns ``(records, continue_from)`` where the returned token can be
    persisted and passed back later to fetch only newer changes. Stops early
    once ``max_records`` is reached (the token still points at the next batch).
    """
    collected: list[dict[str, Any]] = []
    token = continue_from
    while True:
        envelope = fetch_export_page(client, module, feed, token, params=params)
        collected.extend(envelope.get("data", []))
        token = envelope.get("continueFrom", token)
        if max_records is not None and len(collected) >= max_records:
            break
        if not envelope.get("hasMore", False):
            break
    return collected, token

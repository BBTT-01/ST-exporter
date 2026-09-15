"""Pricebook catalogue fetches: full list every run, no cursor.

Pricebook is a catalogue, not a change feed — the tabs are a full replace every
run (see ``CONTRACT-pricebook-tabs.md``), so there is no window and no cursor and
nothing to cache between runs. That makes these plain paginated list calls
against ``pricebook/v2/tenant/{id}/{resource}``, like ``reference.py``'s lookups
rather than like the five ``export/{feed}`` deltas.

Two ServiceTitan quirks are encoded here, both carried over from TrueQuote's own
client (``packages/servicetitan/src/server.ts``), where they were learned the hard
way:

1. ``categoryIds`` honours exactly ONE id per request — extra ids are silently
   ignored. So when a category restriction is configured, each id is requested
   **serially** and the results merged by item id. Never batch them.
2. One item can sit in several categories, so the same item comes back from more
   than one of those serial requests. Merging unions its ``categories`` and
   ``assets`` rather than letting the last response win.
"""

from __future__ import annotations

from typing import Any, Iterable

from st_cli.client import ServiceTitanClient
from st_cli.pagination import fetch_all
from st_exporter.pricebook import asset_identifier

MODULE = "pricebook"
ITEM_RESOURCES: tuple[str, ...] = ("services", "equipment", "materials")
CATEGORIES_RESOURCE = "categories"

_PAGE_SIZE = 200


def fetch_pricebook_items(
    client: ServiceTitanClient,
    resource: str,
    *,
    category_ids: Iterable[int | str] = (),
) -> list[dict[str, Any]]:
    """Full list of one pricebook item resource (``services``/``equipment``/``materials``).

    ``active=Any`` because the contract's ``active`` column has to be able to say
    ``false``: ServiceTitan's pricebook list endpoints default to active-only, so
    without this a withdrawn item would silently vanish from the tab instead of
    being exported as ``active=false`` — and consumers must never delete, only
    mark withdrawn. The exact parameter spelling is unverified against a real
    tenant (see KNOWN_UNVERIFIED.md).

    With no ``category_ids`` this is a single unfiltered pass over the whole
    catalogue. With ids, see the module docstring: one request per id, merged.
    """
    ids = [str(category_id) for category_id in category_ids if str(category_id).strip()]
    if not ids:
        return _fetch_pages(client, resource, None)

    merged: dict[str, dict[str, Any]] = {}
    for category_id in ids:
        for record in _fetch_pages(client, resource, category_id):
            _merge_item(merged, record)
    return list(merged.values())


def fetch_pricebook_categories(client: ServiceTitanClient) -> list[dict[str, Any]]:
    """Full list from ``pricebook/v2/tenant/{id}/categories``.

    Unfiltered and un-merged: the categories endpoint has no ``categoryIds``
    filter, so the one-id-per-request quirk does not apply here.
    """
    return _fetch_pages(client, CATEGORIES_RESOURCE, None)


def _fetch_pages(
    client: ServiceTitanClient, resource: str, category_id: str | None
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"active": "Any"}
    if category_id is not None:
        params["categoryIds"] = category_id
    return list(fetch_all(client, MODULE, resource, params=params, page_size=_PAGE_SIZE))


def _merge_item(merged: dict[str, dict[str, Any]], record: dict[str, Any]) -> None:
    """Union one item into ``merged``, keyed by its ServiceTitan id.

    A record with no ``id`` is dropped: it can't be keyed, can't be deduped, and
    would fail the contract's "``st_id`` non-empty" rule anyway.
    """
    raw_id = record.get("id")
    if raw_id is None or str(raw_id) == "":
        return
    key = str(raw_id)
    prior = merged.get(key)
    if prior is None:
        merged[key] = dict(record)
        return
    combined = {**prior, **record}
    combined["categories"] = _union_by_id(prior.get("categories"), record.get("categories"))
    combined["assets"] = _union_assets(prior.get("assets"), record.get("assets"))
    merged[key] = combined


def _union_by_id(*groups: Any) -> list[dict[str, Any]]:
    """First-seen-order union of category objects by their ``id``."""
    seen: dict[str, dict[str, Any]] = {}
    for group in groups:
        for entry in group or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("id"))
            seen.setdefault(key, entry)
    return list(seen.values())


def _union_assets(*groups: Any) -> list[dict[str, Any]]:
    """First-seen-order union of assets by ``id`` when present, else by ``url``.

    Same identity rule as ``pricebook.asset_identifier`` — assets repeat within a
    single payload as well as across the serial category requests, and only one
    of the two dedupes would otherwise catch each case.
    """
    seen: dict[str, dict[str, Any]] = {}
    for group in groups:
        for entry in group or []:
            if not isinstance(entry, dict):
                continue
            identifier = asset_identifier(entry)
            seen.setdefault(identifier or repr(sorted(entry.items(), key=str)), entry)
    return list(seen.values())

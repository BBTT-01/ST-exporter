"""Pricebook catalogue fetches: full list every run, no cursor.

Pricebook is a catalogue, not a change feed — the tabs are a full replace every
run (see ``CONTRACT-pricebook-tabs.md``), so there is no window and no cursor and
nothing to cache between runs. That makes these plain paginated list calls
against ``pricebook/v2/tenant/{id}/{resource}``, like ``reference.py``'s lookups
rather than like the five ``export/{feed}`` deltas.

Three ServiceTitan quirks are encoded here. The first two are carried over from
TrueQuote's own client (``packages/servicetitan/src/server.ts``), where they were
learned the hard way; the third was learned from a live tenant:

1. ``categoryIds`` honours exactly ONE id per request — extra ids are silently
   ignored. So when a category restriction is configured, each id is requested
   **serially** and the results merged by item id. Never batch them.
2. One item can sit in several categories, so the same item comes back from more
   than one of those serial requests. Merging unions its ``categories`` and
   ``assets`` rather than letting the last response win.
3. **``categories`` has two different shapes depending on the resource.** On
   ``services`` it is an array of objects (``SkuCategoryResponse``: ``id``,
   ``name``, ``active``); on ``equipment`` and ``materials`` it is an array of
   bare ``int64`` category ids with no names attached at all. That is not a
   guess — it is what ``tenant-pricebook-v2``'s OpenAPI says
   (``Pricebook.V2.{Service,Equipment,Material}Response.categories``), and it is
   exactly what run ``35134016237`` on ``tr-doorservpro`` showed: ``services``
   exported its category columns while ``equipment`` (10041 rows) and
   ``materials`` (4990 rows) were blank in both, on a tenant with 61 categories.
   Both shapes are normalised here to ``[{"id": …, "name": …}]``, the names being
   looked up from the categories endpoint — so the pure row mapping downstream
   sees ONE shape and the tab contract is unchanged.
"""

from __future__ import annotations

from typing import Any, Iterable

from st_cli.client import ServiceTitanClient
from st_cli.exceptions import STCLIError
from st_cli.pagination import fetch_all
from st_exporter.logging_setup import logger
from st_exporter.pricebook import asset_identifier
from st_exporter.scopes import is_permission_denied

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
        return _name_categories(client, _fetch_pages(client, resource, None))

    merged: dict[str, dict[str, Any]] = {}
    for category_id in ids:
        for record in _fetch_pages(client, resource, category_id):
            _merge_item(merged, record)
    return _name_categories(client, list(merged.values()))


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
    records = fetch_all(client, MODULE, resource, params=params, page_size=_PAGE_SIZE)
    return [_normalise_categories(record) for record in records]


def _normalise_categories(record: dict[str, Any]) -> dict[str, Any]:
    """One item record with its ``categories`` in object form, whatever shape arrived.

    Quirk 3 in the module docstring: ``equipment`` and ``materials`` return bare
    int ids here, ``services`` returns objects. A bare id becomes ``{"id": <id>}``
    — deliberately with NO ``name`` key, which is what marks it as still needing
    one and is what :func:`_name_categories` looks for. A record with no
    ``categories`` key is returned untouched; an already-object entry keeps every
    field ServiceTitan sent. Entries are COPIED rather than reused, so filling a
    name in later can never reach back into a caller's record — the contract
    fixtures are built from module-level dicts that must not drift.
    """
    raw = record.get("categories")
    if not isinstance(raw, list) or not raw:
        return record
    entries: list[Any] = []
    for entry in raw:
        if isinstance(entry, dict):
            entries.append(dict(entry))
        elif isinstance(entry, (int, str)) and not isinstance(entry, bool):
            entries.append({"id": entry})
        # Anything else is dropped: it carries no id, so it cannot key a category.
    return {**record, "categories": entries}


def _name_categories(
    client: ServiceTitanClient, records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fill in the ``name`` of every category entry that arrived as a bare id.

    Costs ONE extra categories list call, and only when the response actually
    carried nameless ids — so ``services`` (which sends full objects) never pays
    it, and neither does a tenant whose catalogue has no category linkage at all.

    A failure to read categories does not fail the item fetch: ``category_ids``
    is still correct and exporting it with blank names beats losing the whole
    tab. The blank-column detector then reports ``category_names`` truthfully.
    """
    if not any(_needs_name(entry) for record in records for entry in _category_entries(record)):
        return records

    try:
        names = category_name_index(fetch_pricebook_categories(client))
    except STCLIError as exc:
        # A 403 here is the tenant's app never having been granted
        # Pricebook -> Categories, which `scopes.py` classifies for the categories
        # TAB and deliberately keeps at INFO — a permission the contractor never
        # bought is not a defect, and warning about it once per item resource
        # would be noise on every run forever. Anything else IS a defect.
        log = logger.info if is_permission_denied(exc) else logger.warning
        log(
            "could not read pricebook categories (%s); category_names will be blank "
            "for the items whose categories arrived as bare ids",
            exc,
        )
        return records

    return apply_category_names(records, names)


def apply_category_names(
    records: list[dict[str, Any]], names: dict[str, str]
) -> list[dict[str, Any]]:
    """Both ``categories`` shapes -> ``[{"id", "name"}]``, names filled from ``names``.

    Pure and client-free, so the contract-fixture generator can put a raw
    ServiceTitan record through exactly the pass a live run puts it through
    instead of hand-writing the already-resolved shape and pinning a fiction.

    An id with no entry in ``names`` gets a blank name rather than being dropped:
    ``category_ids`` and ``category_names`` are index-aligned, so the id keeps its
    slot.
    """
    resolved = [_normalise_categories(record) for record in records]
    for record in resolved:
        for entry in _category_entries(record):
            if _needs_name(entry):
                entry["name"] = names.get(str(entry.get("id")), "")
    return resolved


def category_name_index(categories: list[dict[str, Any]]) -> dict[str, str]:
    """``{category id as text: name}`` over the category list, subcategories included.

    ``Pricebook.V2.CategoryResponse`` can nest ``subcategories``, and an item may
    reference a nested id, so the index walks the tree rather than the top level
    only. First spelling of an id wins; the ids are keyed as text so an ``int``
    from one endpoint and a ``str`` from another resolve to the same category.
    """
    index: dict[str, str] = {}
    pending = list(categories or [])
    while pending:
        category = pending.pop(0)
        if not isinstance(category, dict):
            continue
        identifier = str(category.get("id") or "").strip()
        if identifier:
            index.setdefault(identifier, str(category.get("name") or ""))
        children = category.get("subcategories")
        if isinstance(children, list):
            pending.extend(children)
    return index


def _category_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    raw = record.get("categories")
    return [entry for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []


def _needs_name(entry: dict[str, Any]) -> bool:
    return entry.get("id") is not None and not str(entry.get("name") or "").strip()


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

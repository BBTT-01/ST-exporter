"""Pure record -> row mapping for the four `pricebook.*` tabs.

Kept deliberately free of HTTP and Sheets so the exact grid a run would write can
be produced from a list of ServiceTitan records alone — that is what makes the
output recordable as a fixture (ticket 04) and what every test in
``tests/st_exporter/test_pricebook.py`` exercises.

The column sets and cell rules come from ``CONTRACT-pricebook-tabs.md``
(`pricebook.v2`). The rules that are easiest to break, and are
therefore enforced here in one place:

- ``st_id`` is never blank: a record with no ServiceTitan id is dropped rather
  than written as a keyless row that ``row_count`` would nevertheless count.
- Every cell is text. A blank cell means empty; a missing column means absent.
  Those are never collapsed — in particular ``price``, ``cost`` and ``hours`` are
  **blank when null, never ``0``**, because a real zero-cost item and an item
  with no cost recorded are different facts to the consumer: one is free, the
  other is unknown, and a pricing formula fed the first when it meant the second
  prices the job at pure margin.
- ``name`` prefers ``displayName``, falls back to ``name``, and is never blank.
- ``category_ids`` and ``category_names`` are comma-separated in the SAME order,
  index for index.
- ``image_refs`` carries identifiers only — never bytes — deduped by asset id,
  because assets repeat within a single payload.

- ``cost`` and ``hours`` come from ServiceTitan's own ``cost`` and ``hours``
  fields on the pricebook item. ``hours`` exists on all three item resources;
  ``cost`` exists on ``equipment`` and ``materials`` and **not on ``services``**
  (ServiceTitan's ``Pricebook.V2.ServiceResponse`` has no cost field at all — a
  service's cost is its labour). So ``pricebook.services.cost`` is blank on
  every row of every tenant, by construction rather than by accident, and
  ``blank_columns`` exempts it for that reason.

``services``, ``equipment`` and ``materials`` share one shape and therefore one
code path: the tab name carries the meaning, the parser does not have to.
"""

from __future__ import annotations

from typing import Any

from st_exporter.format import to_cell_text

CONTRACT_VERSION = "pricebook.v2"

ITEM_COLUMNS: tuple[str, ...] = (
    "st_id",
    "code",
    "name",
    "description",
    "price",
    "active",
    "category_ids",
    "category_names",
    "manufacturer",
    "model",
    "image_refs",
    "modified_on",
    "cost",
    "hours",
)

CATEGORY_COLUMNS: tuple[str, ...] = ("st_id", "name", "active", "parent_id")


def build_item_grid(records: list[dict[str, Any]]) -> list[list[str]]:
    """Header row + one row per item with an ``st_id``, for any of the three item tabs.

    A record with no id is DROPPED here rather than written as a row with a blank
    key. The contract says ``st_id`` is non-empty, a consumer keyed on it would
    discard the row anyway, and — the reason it matters — a blank key column in a
    written tab is indistinguishable from real data at a glance, while the run's
    ``row_count`` would still have counted it. The category-filtered fetch path
    already drops id-less records when it merges them (``feeds/pricebook``); this
    is the same rule on the unfiltered path, applied where every path meets.
    """
    rows = [build_item_row(r) for r in records]
    return [list(ITEM_COLUMNS)] + [format_item_row(row) for row in rows if row["st_id"]]


def build_category_grid(records: list[dict[str, Any]]) -> list[list[str]]:
    """Header row + one row per category with an ``st_id``, for `pricebook.categories`.

    Same non-empty-key rule as :func:`build_item_grid`.
    """
    rows = [build_category_row(r) for r in records]
    return [list(CATEGORY_COLUMNS)] + [format_category_row(row) for row in rows if row["st_id"]]


def build_item_row(record: dict[str, Any]) -> dict[str, str]:
    """Map one ServiceTitan pricebook item onto the item-tab columns."""
    category_ids, category_names = _categories(record.get("categories"))
    return {
        "st_id": to_cell_text(record.get("id")),
        "code": to_cell_text(record.get("code")),
        "name": _name(record),
        "description": to_cell_text(record.get("description")),
        "price": _numeric(record.get("price")),
        "active": _bool_text(record.get("active")),
        "category_ids": category_ids,
        "category_names": category_names,
        "manufacturer": to_cell_text(record.get("manufacturer")),
        "model": to_cell_text(record.get("model")),
        "image_refs": image_refs(record.get("assets")),
        "modified_on": to_cell_text(record.get("modifiedOn")),
        "cost": _numeric(record.get("cost")),
        "hours": _numeric(record.get("hours")),
    }


def build_category_row(record: dict[str, Any]) -> dict[str, str]:
    """Map one ServiceTitan pricebook category onto the category-tab columns.

    ``parent_id`` is blank at the top level — ServiceTitan returns ``null`` there
    and the contract keeps it blank rather than inventing a sentinel.
    """
    return {
        "st_id": to_cell_text(record.get("id")),
        "name": to_cell_text(record.get("name")),
        "active": _bool_text(record.get("active")),
        "parent_id": to_cell_text(record.get("parentId")),
    }


def format_item_row(row: dict[str, str]) -> list[str]:
    return [row.get(column, "") for column in ITEM_COLUMNS]


def format_category_row(row: dict[str, str]) -> list[str]:
    return [row.get(column, "") for column in CATEGORY_COLUMNS]


def _name(record: dict[str, Any]) -> str:
    """``displayName``, else ``name`` — and never blank.

    The contract states this column is never blank. ServiceTitan can in principle
    return both as null, so rather than emit a blank cell (which would mean
    "empty" to the reader and silently break the guarantee) this falls back to
    ``code`` and then to the item id, both of which are always present on a row
    that got this far. Flagged in KNOWN_UNVERIFIED.md.
    """
    for candidate in (record.get("displayName"), record.get("name"), record.get("code")):
        text = to_cell_text(candidate).strip()
        if text:
            return text
    return to_cell_text(record.get("id"))


def _numeric(value: Any) -> str:
    """``price`` / ``cost`` / ``hours`` as text — **blank when null, never ``0``**.

    A genuine zero still renders as ``"0"``; only an absent/null value is blank.
    Collapsing the two would tell the consumer a priceless item is free, a
    costless item free to buy, and an hourless item instant to fit.
    """
    if value is None:
        return ""
    return to_cell_text(value)


def _bool_text(value: Any) -> str:
    """Lowercase ``true``/``false``; blank when the field is absent.

    Blank rather than defaulting to ``false``, because ``false`` means "withdrawn"
    to the consumer and guessing it would retire a live item.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return to_cell_text(value)
    text = str(value).strip().lower()
    if text in {"true", "false"}:
        return text
    return ""


def _categories(categories: Any) -> tuple[str, str]:
    """Return ``(category_ids, category_names)`` as index-aligned CSV strings.

    An entry with no id is skipped entirely rather than contributing a name with
    no id beside it — the two columns must stay in the same order, index for
    index, and a half-entry is what would break that.

    A **bare id** (``categories: [123, 456]``) is accepted as well as an object:
    that is the shape ``equipment`` and ``materials`` actually return, per
    ``tenant-pricebook-v2``'s OpenAPI, and reading only the object form is what
    blanked both columns on 15031 rows of ``tr-doorservpro``. Such an entry's
    name is blank here — ``feeds/pricebook`` resolves names off the categories
    endpoint before the records reach this function — because ``category_ids``
    alone is still a usable join key, and skipping the entry would lose that too.
    """
    ids: list[str] = []
    names: list[str] = []
    for entry in categories or []:
        if isinstance(entry, (int, str)) and not isinstance(entry, bool):
            entry = {"id": entry}
        if not isinstance(entry, dict):
            continue
        identifier = to_cell_text(entry.get("id")).strip()
        if not identifier:
            continue
        ids.append(identifier)
        names.append(to_cell_text(entry.get("name")).strip())
    return ",".join(ids), ",".join(names)


def asset_identifier(asset: dict[str, Any]) -> str:
    """The stable identity of one asset: its ``id`` when ServiceTitan supplies one,
    otherwise its ``url``.

    ``url`` is either a public ``https://`` URL or an authenticated storage path
    such as ``Images/Pricebook/<uuid>.jpg``. Both forms are carried through
    verbatim as identifiers — the Sheet never holds bytes, and the image-download
    ticket is what has to tell the two apart when it fetches them (an HTTPS URL is
    fetched directly; a storage path goes to
    ``pricebook/v2/tenant/{id}/images?path=…``).
    """
    identifier = to_cell_text(asset.get("id")).strip()
    if identifier:
        return identifier
    return to_cell_text(asset.get("url")).strip()


def image_refs(assets: Any) -> str:
    """Comma-separated asset identifiers, deduped, first-seen order preserved.

    Assets repeat within a single ServiceTitan payload (and again across the
    serial per-category requests), so the dedupe is mandatory, not tidiness.
    """
    seen: list[str] = []
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        identifier = asset_identifier(asset)
        if identifier and identifier not in seen:
            seen.append(identifier)
    return ",".join(seen)

"""Rich table + JSON output formatting."""

from __future__ import annotations

import json
from typing import Any, Sequence

from rich.console import Console
from rich.table import Table

Column = tuple[str, str]  # (display_name, api_field_key)

console = Console()


def _resolve(record: dict[str, Any], key: str) -> Any:
    """Resolve a column key against one record.

    Three forms, each there for a reason a column once went permanently blank:

    - ``'address.city'`` — a nested path.
    - ``'phoneSettings.0.phone'`` — a numeric segment indexes a list, because
      ServiceTitan returns a customer's contact details as arrays of setting
      objects rather than as scalars.
    - ``'jobNumber|number'`` — alternatives, left to right, first NON-EMPTY
      wins. This is how a spelling is WIDENED without narrowing: the new, right
      spelling goes first and the old one stays as a fallback, so no tenant that
      worked before can stop working.

    "Non-empty" rather than "not ``None``" because ServiceTitan really does
    return ``phoneSettings: [{"phone": ""}]`` alongside a populated flat
    ``phone``, and letting the blank win hides a number that is right there —
    the same rule ``st_exporter.denormalize._contact_detail`` already applies.
    A value that is empty everywhere is still returned (as the first one seen)
    so "present but blank" stays distinguishable from "absent".

    **Two implementations of one rule, and they are deliberately not identical.**
    ``denormalize._contact_detail`` applies the same "first non-empty" rule to
    the same ServiceTitan shapes, and the two have drifted apart once already —
    that drift *was* the bug round two fixed. They are kept separate (this one
    is a display-time path resolver for arbitrary column keys; that one knows
    what a customer contact is), so the agreement is pinned by a test instead:
    ``tests/test_contact_resolution_parity.py`` runs both over one shared matrix.

    The differences that test asserts are INTENDED, rather than more drift:

    - **Array position.** ``'phoneSettings.0.phone'`` reads index 0 and stops;
      ``_contact_detail`` scans every entry for the first non-empty one. That is
      on purpose. A column key is a literal path a human wrote and must resolve
      to exactly what it says, and the CLI table is a debugging view where
      "index 0 is blank" is itself the useful fact. The exporter's tab is a
      frozen contract column read by another system, where a blank cell is
      indistinguishable from a contractor with no phone number, so it is worth
      scanning for. A key may always spell ``.1.`` explicitly.
    - **Spelling order.** The alternation order here is the caller's (see
      ``crm.CUSTOMER_COLUMNS``); ``fields`` order is the exporter's. Both are
      widen-only, so order only decides between two populated values.
    - **``contacts[]``.** ``_contact_detail`` also selects ``contacts[]`` by
      ``type``; no path DSL can express "the entry whose type is Phone", so this
      resolver has no equivalent and the CLI's phone/email columns can still be
      blank where the exporter's are not.
    - **A blank array entry with nothing beside it.** ``{"phoneSettings":
      [{"phone": ""}]}`` resolves to ``''`` here (the named path holds a blank,
      and "present but blank" is worth seeing) and to ``None`` in
      ``_contact_detail`` (which found no non-empty entry and has no flat key to
      fall back to). Both render as an empty cell, so nothing downstream can tell
      them apart — but it is a real difference and it is pinned, because an
      unexplained disagreement between these two is exactly what the round-two
      bug looked like before anyone noticed.

    FOUR differences, not three. Anything else is drift.
    """
    first_seen: Any = None
    seen = False
    for alternative in key.split("|"):
        value = _resolve_one(record, alternative)
        if value is None:
            continue
        if str(value).strip():
            return value
        if not seen:
            first_seen, seen = value, True
    return first_seen if seen else None


def _resolve_one(record: dict[str, Any], key: str) -> Any:
    val: Any = record
    for part in key.split("."):
        if isinstance(val, dict):
            val = val.get(part)
        elif isinstance(val, list) and part.isdigit():
            index = int(part)
            val = val[index] if index < len(val) else None
        else:
            return None
    return val


def render(
    data: list[dict[str, Any]],
    columns: Sequence[Column],
    *,
    as_json: bool = False,
    title: str | None = None,
    total_count: int | None = None,
) -> None:
    """Render a list of records as a table or JSON."""
    if as_json:
        console.print_json(json.dumps(data, default=str))
        return

    table = Table(title=title, show_lines=False)
    for display_name, _ in columns:
        table.add_column(display_name)

    for record in data:
        row = [str(_resolve(record, key) or "") for _, key in columns]
        table.add_row(*row)

    console.print(table)
    if total_count is not None:
        console.print(f"[dim]Total: {total_count}[/dim]")


def render_single(
    record: dict[str, Any],
    columns: Sequence[Column],
    *,
    as_json: bool = False,
) -> None:
    """Render a single record as a vertical key-value table or JSON."""
    if as_json:
        console.print_json(json.dumps(record, default=str))
        return

    table = Table(show_header=False, show_lines=True)
    table.add_column("Field", style="bold")
    table.add_column("Value")

    for display_name, key in columns:
        val = _resolve(record, key)
        table.add_row(display_name, str(val) if val is not None else "")

    console.print(table)

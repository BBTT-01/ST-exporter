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

"""Human-friendly date range parser."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from st_cli.exceptions import DateParseError


def apply_date_params(
    params: dict[str, Any],
    range_val: str | None,
    from_date: str | None,
    to_date: str | None,
    start_key: str = "createdOnOrAfter",
    end_key: str = "createdBefore",
) -> None:
    """Apply date range params to a query dict. Explicit from/to override --range.

    Default keys match the ServiceTitan "created" convention. Pass different
    keys (e.g. ``startsOnOrAfter`` / ``startsBefore``) for dispatch endpoints.
    """
    if from_date:
        params[start_key] = from_date
    if to_date:
        params[end_key] = to_date
    if range_val and not (from_date or to_date):
        start, end = parse_date_range(range_val)
        params[start_key] = start.isoformat()
        params[end_key] = end.isoformat()


def parse_date_range(value: str) -> tuple[date, date]:
    """Parse a human-friendly date range string into (from_date, to_date).

    Supported formats:
      today, yesterday, tomorrow
      this-week, last-week, next-week
      this-month, last-month, next-month
      this-quarter, last-quarter, next-quarter
      this-year, last-year
      last-7-days, last-30-days, last-90-days
      2025-01-01            (single day)
      2025-01-01:2025-01-31 (explicit range)
    """
    today = date.today()
    val = value.strip().lower()

    # Relative days
    if val == "today":
        return today, today
    if val == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    if val == "tomorrow":
        d = today + timedelta(days=1)
        return d, d

    # last-N-days
    m = re.match(r"^last-(\d+)-days$", val)
    if m:
        n = int(m.group(1))
        return today - timedelta(days=n), today

    # this/last/next week (Monday start)
    if val in ("this-week", "last-week", "next-week"):
        monday = today - timedelta(days=today.weekday())
        if val == "last-week":
            monday -= timedelta(weeks=1)
        elif val == "next-week":
            monday += timedelta(weeks=1)
        return monday, monday + timedelta(days=6)

    # this/last/next month
    if val in ("this-month", "last-month", "next-month"):
        year, month = today.year, today.month
        if val == "last-month":
            month -= 1
            if month < 1:
                month = 12
                year -= 1
        elif val == "next-month":
            month += 1
            if month > 12:
                month = 1
                year += 1
        first = date(year, month, 1)
        # last day: first of next month minus 1 day
        if month == 12:
            last = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last = date(year, month + 1, 1) - timedelta(days=1)
        return first, last

    # this/last/next quarter
    if val in ("this-quarter", "last-quarter", "next-quarter"):
        q = (today.month - 1) // 3  # 0-indexed quarter
        year = today.year
        if val == "last-quarter":
            q -= 1
            if q < 0:
                q = 3
                year -= 1
        elif val == "next-quarter":
            q += 1
            if q > 3:
                q = 0
                year += 1
        first_month = q * 3 + 1
        last_month = first_month + 2
        first = date(year, first_month, 1)
        if last_month == 12:
            last = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last = date(year, last_month + 1, 1) - timedelta(days=1)
        return first, last

    # this/last year
    if val == "this-year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if val == "last-year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)

    # Explicit: single date or range. Accept ".." as a separator alias — LLM callers
    # routinely emit "2025-01-01..2025-01-31" since it's the common range convention.
    if ".." in val:
        val = val.replace("..", ":", 1)
    if ":" in val:
        parts = val.split(":", 1)
        try:
            return date.fromisoformat(parts[0]), date.fromisoformat(parts[1])
        except ValueError as exc:
            raise DateParseError(f"Invalid date range: {value}") from exc

    # Single date
    try:
        d = date.fromisoformat(val)
        return d, d
    except ValueError:
        pass

    raise DateParseError(
        f"Unknown date range '{value}'. Try: today, last-week, last-30-days, "
        f"this-month, 2025-01-01, or 2025-01-01:2025-01-31"
    )


def validate_max_range(
    params: dict[str, Any],
    start_key: str,
    end_key: str,
    max_days: int,
) -> None:
    """Raise DateParseError if the resolved date range in *params* exceeds *max_days*."""
    start_val = params.get(start_key)
    end_val = params.get(end_key)
    if start_val and end_val:
        start_d = date.fromisoformat(str(start_val))
        end_d = date.fromisoformat(str(end_val))
        delta = (end_d - start_d).days
        if delta > max_days:
            raise DateParseError(f"Date range spans {delta} days; maximum is {max_days} days")

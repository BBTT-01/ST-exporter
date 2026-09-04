"""The Export Store window: which jobs rows belong in the Sheet.

A jobs row belongs in the Sheet when its appointment falls within the last 90 days,
or is in the future. Measured on the appointment's own start time, never on the
job's creation date and never on last-modified — a job created five months ago and
scheduled for today must still appear.

Because "the last 90 days" already includes every date up to and including today,
"or is in the future" adds no additional dates on its own — the two clauses collapse
into one inequality. It's called out explicitly in the source ticket to rule out a
wrongly *symmetric* window (``today - 90d <= start <= today + 90d``), which would
incorrectly exclude a job scheduled further out than 90 days from now.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

DEFAULT_WINDOW_DAYS = 90


def in_window(
    appointment_start: str,
    *,
    today: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> bool:
    """True if ``appointment_start`` (ISO 8601, with offset) belongs in the Sheet.

    ``today`` is threaded in rather than computed here so every row evaluated in
    one run shares exactly the same cutoff. A malformed/unparsable timestamp is
    treated as excluded rather than raised — one bad upstream record must not be
    able to abort the whole run (the crash would happen before any write, so the
    next run would re-fetch and re-crash on the same record forever). The caller
    (``run.py``) is responsible for counting/logging rows dropped this way.
    """
    try:
        start_date = _parse_utc_date(appointment_start)
    except (ValueError, TypeError):
        return False
    return start_date >= today - timedelta(days=window_days)


def _parse_utc_date(iso_timestamp: str) -> date:
    """Parse an ISO 8601 timestamp and return its UTC calendar date.

    Converting to UTC before taking the date component keeps the boundary decision
    independent of which offset ServiceTitan happens to report for a given
    appointment — otherwise the same instant could land on different sides of the
    cutoff depending on timezone alone.
    """
    dt = datetime.fromisoformat(iso_timestamp)
    if dt.tzinfo is None:
        # No offset present — treat as already UTC rather than guessing a local zone.
        return dt.date()
    return dt.astimezone(timezone.utc).date()

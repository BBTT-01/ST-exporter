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


# ---------------------------------------------------------------------------
# The financial window — a SEPARATE decision from DEFAULT_WINDOW_DAYS above
# ---------------------------------------------------------------------------

#: How far back the `financial` feed reaches: invoices, job timesheets and the
#: Job Costing Summary report.
#:
#: **This is 90 for an entirely different reason than the jobs window is 90, and
#: the two must never be collapsed into one constant.** The jobs window is 90
#: because a five-month-old job scheduled for today has to appear on a
#: technician's screen; nothing about that applies to an invoice. This one is 90
#: because it is the shortest window that still covers every Profit Wizard
#: surface fed from ServiceTitan — measured against Profit Wizard's own code
#: rather than picked as a round number:
#:
#:   - ``lib/company/operating-metrics.ts`` — ``OPERATING_METRIC_WINDOW_DAYS = 90``
#:     (warranty %, financing %)
#:   - ``lib/services/safety-analysis.ts`` — ``ANALYSIS_WINDOW_DAYS = 90``, with
#:     7/30/90 rolling buckets
#:   - ``app/api/cron/sync-job-costs/route.ts`` — the six-hourly cron pulls
#:     invoices, hours and quotes with ``maxLookbackDays: 90``
#:   - the dashboard's health margin (30 days + last calendar month), the
#:     technicians page (30) and goals (month-to-date) all sit inside 90
#:
#: The shortest thing that covers all of those is 90 days, so 90 it is. Anything
#: shorter silently truncates the 90-day buckets the safety engine and the
#: operating metrics are built on.
#:
#: Two known gaps, deliberate:
#:   - Profit Wizard's reports page offers a 365-day ("Last year") timeframe.
#:     That is NOT covered here; covering it would quadruple a six-hourly pull to
#:     serve one optional picker value. Raise ``EXPORTER_FINANCIAL_WINDOW_DAYS``
#:     to 365 for a tenant that needs it.
#:   - The 12-month forecasting/variable-calculator inputs and the 36-month
#:     QuickBooks history are fed from QuickBooks, not ServiceTitan, so they are
#:     not this feed's problem.
#:
#: The ServiceTitan job-cost sync's own default in Profit Wizard is 45 days
#: (``cost-sync-schedule.ts``, ``maxLookbackDays = 45``); 45 is not enough for
#: the feed as a whole because the same tabs feed the 90-day surfaces above.
FINANCIAL_WINDOW_DAYS = 90

"""Fetch for the `dispatch.nonJobAppointments` tab: non-job appointments + code names.

=================================  ====================================================
what                               endpoint
=================================  ====================================================
the tab's records                  ``dispatch/v2/tenant/{id}/non-job-appointments``
``timesheet_code_name`` lookup     ``payroll/v2/tenant/{id}/timesheet-codes``
=================================  ====================================================

Module routing was checked against ``st_cli/registry.py`` (``non-job-appointments``
under ``dispatch``, ``timesheet-codes`` under ``payroll``) rather than assumed.

**A bounded window, sent server-side.** Non-job appointments accumulate forever
and the dispatch board looks at days around today, so the list is filtered to
``window.DISPATCH_LOOKBACK_DAYS`` back and ``window.DISPATCH_LOOKAHEAD_DAYS``
ahead with ``startsOnOrAfter`` / ``startsOnOrBefore``, both honoured on a live
tenant, and ``activeOnly=true``, which drops exactly the ``active=false`` rows.
ServiceTitan silently IGNORES a misspelled parameter (``startsBefore`` and
``active=True`` were both ignored on the same probe) and answers with the whole
history, so :func:`warn_if_outside_window` checks every record against the window
that was asked for.

**The code names are optional.** ``timesheet_code_name`` is a convenience column:
a tenant whose app was not granted ``Payroll -> Timesheet Codes`` still gets
every row, with that one column blank. A 403 there is that ordinary state and is
logged at INFO with no annotation, so it cannot put a yellow mark on every run of
every such tenant; any other failure degrades the same way but is announced.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from st_cli.client import ServiceTitanClient
from st_cli.exceptions import STCLIError
from st_cli.pagination import fetch_all
from st_exporter.dispatch import TIMESHEET_CODE_NAME_FIELD, timesheet_code_id
from st_exporter.logging_setup import announce_to_actions, logger
from st_exporter.scopes import is_permission_denied
from st_exporter.window import DISPATCH_LOOKAHEAD_DAYS, DISPATCH_LOOKBACK_DAYS

MODULE = "dispatch"
RESOURCE = "non-job-appointments"
TIMESHEET_CODES_MODULE = "payroll"
TIMESHEET_CODES_RESOURCE = "timesheet-codes"

START_PARAM = "startsOnOrAfter"
END_PARAM = "startsOnOrBefore"

_PAGE_SIZE = 200


def window_bounds(
    today: date,
    *,
    lookback_days: int = DISPATCH_LOOKBACK_DAYS,
    lookahead_days: int = DISPATCH_LOOKAHEAD_DAYS,
) -> tuple[datetime, datetime]:
    """UTC midnight ``lookback_days`` before ``today`` and ``lookahead_days`` after it.

    Midnights rather than "now plus or minus N days" so every run inside one UTC
    day asks for exactly the same range — the same reason
    ``feeds.financial.window_start`` uses one.
    """
    start = datetime.combine(today - timedelta(days=lookback_days), time.min, tzinfo=timezone.utc)
    end = datetime.combine(today + timedelta(days=lookahead_days), time.min, tzinfo=timezone.utc)
    return start, end


def fetch_non_job_appointments(
    client: ServiceTitanClient,
    *,
    today: date,
) -> list[dict[str, Any]]:
    """Active non-job appointments starting inside the window, code names stamped on.

    The appointments are listed first and the code names second, so a tenant
    refused the appointments themselves is refused before any payroll call is
    made. Every record leaves with :data:`TIMESHEET_CODE_NAME_FIELD` set: the
    code's name, or ``None`` when the record has no code or it did not resolve.
    """
    start, end = window_bounds(today)
    records = list(
        fetch_all(
            client,
            MODULE,
            RESOURCE,
            params={START_PARAM: _iso(start), END_PARAM: _iso(end), "activeOnly": "true"},
            page_size=_PAGE_SIZE,
        )
    )
    warn_if_outside_window(records, start=start, end=end)
    names = fetch_timesheet_code_names(client) if _any_code(records) else {}
    return [
        {
            **record,
            TIMESHEET_CODE_NAME_FIELD: names.get(timesheet_code_id(record.get("timesheetCodeId"))),
        }
        for record in records
    ]


def fetch_timesheet_code_names(client: ServiceTitanClient) -> dict[str, str]:
    """Timesheet code id -> its ``code``, retired codes included; ``{}`` if refused.

    ``active=Any`` because an appointment can still carry a code that has since
    been retired, and its name is no less true for that.
    """
    try:
        records = list(
            fetch_all(
                client,
                TIMESHEET_CODES_MODULE,
                TIMESHEET_CODES_RESOURCE,
                params={"active": "Any"},
                page_size=_PAGE_SIZE,
            )
        )
    except STCLIError as exc:
        if is_permission_denied(exc):
            logger.info(
                "dispatch: ServiceTitan answered 403 for payroll timesheet codes, so this "
                "tenant's app was not granted Payroll -> Timesheet Codes. "
                "dispatch.nonJobAppointments is still written, with timesheet_code_name "
                "blank."
            )
            return {}
        logger.warning(
            "DEGRADED: could not read payroll timesheet codes (%s). "
            "dispatch.nonJobAppointments is still written, with timesheet_code_name blank.",
            exc,
        )
        announce_to_actions(
            "Timesheet codes degraded",
            f"could not read payroll timesheet codes ({exc}). dispatch.nonJobAppointments "
            f"still exported; timesheet_code_name is blank on every row this run.",
        )
        return {}
    names: dict[str, str] = {}
    for record in records:
        code_id = timesheet_code_id(record.get("id"))
        code = record.get("code")
        if code_id and code is not None and str(code).strip():
            names[code_id] = str(code)
    return names


def warn_if_outside_window(
    records: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> None:
    """Log loudly when a record's ``start`` falls outside the window asked for.

    The same tripwire as ``feeds.financial.warn_if_older_than_window``, checked
    at both ends because both filters can be lost to a misspelling. It only logs:
    dropping the rows locally would hide the one symptom that proves the
    server-side filter stopped working.
    """
    moments = [moment for moment in (_moment(record.get("start")) for record in records) if moment]
    if not moments:
        return
    earliest, latest = min(moments), max(moments)
    if earliest >= start and latest <= end:
        return
    logger.warning(
        "dispatch: non-job appointments came back starting between %s and %s, outside "
        "the requested window %s to %s. ServiceTitan ignores query parameters it does "
        "not recognise, so %r / %r may no longer be honoured and this tab may hold the "
        "tenant's entire history. Verify them against the live API.",
        earliest.isoformat(),
        latest.isoformat(),
        start.isoformat(),
        end.isoformat(),
        START_PARAM,
        END_PARAM,
    )


def _any_code(records: list[dict[str, Any]]) -> bool:
    return any(timesheet_code_id(record.get("timesheetCodeId")) for record in records)


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")

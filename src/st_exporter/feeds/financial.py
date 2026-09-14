"""Fetches for the four `financial` tabs: invoices, timesheets, business units, job costs.

Module routing was checked against ``st_cli/registry.py`` rather than assumed —
``CLAUDE.md`` records two prior bugs of exactly this kind (estimates in
``salestech``, job types in ``jpm``):

===========================  =====================================================
tab                          endpoint
===========================  =====================================================
``accounting.invoices``      ``accounting/v2/tenant/{id}/invoices``
``payroll.timesheets``       ``payroll/v2/tenant/{id}/jobs/{jobId}/timesheets``
``settings.businessUnits``   ``settings/v2/tenant/{id}/business-units``
``reporting.jobCosts``       the built-in Job Costing Summary report (reporting.py)
===========================  =====================================================

**The timesheets endpoint is job-scoped, and that is not a choice.** The registry
also exposes a bulk ``payroll/timesheets`` list, but it answers with the PAYROLL
timesheet shape — ``employeeId``/``activityCodeId``/``startedOn``/``endedOn`` —
and Profit Wizard's reader wants the DISPATCH shape:
``jobId``/``technicianId``/``arrivedOn``/``doneOn``/``canceledOn``. Only
``jobs/{jobId}/timesheets`` returns that, one job at a time, which is exactly what
Profit Wizard itself calls. So this feed lists the jobs completed inside the
window and then asks for each one's timesheets — N+1 by necessity, bounded by
``max_jobs``, and affordable only because this feed runs six-hourly rather than
every five minutes.

Invoices are **not a catalogue and they grow**, so unlike the pricebook feed they
are window-bounded — see ``window.FINANCIAL_WINDOW_DAYS`` for the window and the
reason for its length. Business units are a small reference table (dozens of rows)
and get the full-refresh treatment ``reference.py`` gives technicians and job types.

The date filters are applied **server-side**, not by fetching everything and
discarding rows locally: these endpoints grow without bound and the whole point of
choosing a window is to not pull years of invoices every six hours.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from st_cli.client import ServiceTitanClient
from st_cli.exceptions import APIError
from st_cli.pagination import fetch_all
from st_exporter.feeds import reporting
from st_exporter.logging_setup import logger
from st_exporter.window import FINANCIAL_WINDOW_DAYS

INVOICES_MODULE = "accounting"
INVOICES_RESOURCE = "invoices"
TIMESHEETS_MODULE = "payroll"
BUSINESS_UNITS_MODULE = "settings"
BUSINESS_UNITS_RESOURCE = "business-units"
JOBS_MODULE = "jpm"
JOBS_RESOURCE = "jobs"

#: An invoice belongs in the window by the date it was invoiced, and a job by the
#: date it completed — the business dates Profit Wizard reports on, not
#: ``createdOn`` (a back-dated invoice entered today is revenue for its invoice
#: date) and not ``modifiedOn`` (a note edited today does not move a job's cost
#: into this month). Profit Wizard uses ``invoicedOnOrAfter`` and filters jobs on
#: ``completed_date``; the exact spellings are unverified against a real tenant —
#: see KNOWN_UNVERIFIED.md.
INVOICE_DATE_PARAM = "invoicedOnOrAfter"
JOB_COMPLETED_PARAM = "completedOnOrAfter"

_PAGE_SIZE = 200

#: Timesheets cost one request per job, so an unbounded job list is an unbounded
#: request count against an API that throttles. Profit Wizard caps its own
#: equivalent pass at 150 jobs per run; this is more generous because the
#: exporter has no per-job "already have hours" state to skip on, but it is still
#: a cap, and hitting it is logged rather than silently truncating.
DEFAULT_MAX_TIMESHEET_JOBS = 500


def window_start(today: date, *, window_days: int = FINANCIAL_WINDOW_DAYS) -> datetime:
    """The inclusive start of the financial window, as a UTC instant.

    Midnight UTC on the cutoff date rather than "now minus N days" so every run
    inside the same day asks ServiceTitan for exactly the same range — otherwise
    a row could enter and leave the tab between two six-hourly runs purely
    because the clock moved, which looks like data loss to whoever reads it.
    """
    return datetime.combine(today - timedelta(days=window_days), time.min, tzinfo=timezone.utc)


def fetch_invoices(
    client: ServiceTitanClient,
    *,
    today: date,
    window_days: int = FINANCIAL_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Invoices invoiced on or after the window's start, line items included."""
    params = {INVOICE_DATE_PARAM: _iso(window_start(today, window_days=window_days))}
    return list(
        fetch_all(client, INVOICES_MODULE, INVOICES_RESOURCE, params=params, page_size=_PAGE_SIZE)
    )


def fetch_business_units(client: ServiceTitanClient) -> list[dict[str, Any]]:
    """Full list from ``settings/v2/tenant/{id}/business-units`` — no window.

    A reference table, so this is a full replace every run exactly like
    ``reference.fetch_business_units``, which hits the same endpoint but returns
    the records keyed by id for the jobs feed's denormalisation join. Same
    endpoint, same records, two shapes; this one is the list the tab is built
    from. ``active=Any`` so a retired unit exports as ``Active=false`` instead of
    vanishing — historical invoices still point at it.
    """
    return list(
        fetch_all(
            client,
            BUSINESS_UNITS_MODULE,
            BUSINESS_UNITS_RESOURCE,
            params={"active": "Any"},
            page_size=_PAGE_SIZE,
        )
    )


def fetch_completed_job_ids(
    client: ServiceTitanClient,
    *,
    today: date,
    window_days: int = FINANCIAL_WINDOW_DAYS,
    max_jobs: int = DEFAULT_MAX_TIMESHEET_JOBS,
) -> list[str]:
    """Ids of jobs completed inside the window, newest-completing first, capped.

    Only the ids are kept: this list exists purely to drive the per-job timesheet
    calls, and the `jobs` tab is a different feed with a different window.
    """
    params = {
        JOB_COMPLETED_PARAM: _iso(window_start(today, window_days=window_days)),
        "sort": "-completedOn",
    }
    job_ids: list[str] = []
    for record in fetch_all(
        client, JOBS_MODULE, JOBS_RESOURCE, params=params, page_size=_PAGE_SIZE
    ):
        job_id = record.get("id")
        if job_id is None or str(job_id) == "":
            continue
        job_ids.append(str(job_id))
        if len(job_ids) >= max_jobs:
            logger.warning(
                "financial: stopped listing completed jobs at the %d-job cap; "
                "payroll.timesheets covers the most recently completed jobs only. "
                "Raise EXPORTER_FINANCIAL_MAX_JOBS if this tenant needs more.",
                max_jobs,
            )
            break
    return job_ids


def fetch_timesheets(
    client: ServiceTitanClient,
    job_ids: list[str],
) -> list[dict[str, Any]]:
    """Timesheet segments for each job id, flattened into one list.

    One request per job — see the module docstring for why there is no bulk form
    with these fields. ``jobId`` is stamped onto every segment because the
    job-scoped response does not always carry it back and the tab's key column
    would otherwise be blank for every row.

    A 404 on one job is skipped, not raised: a job can be deleted between the
    list call and this one, and losing the entire tab over one missing job would
    be a self-inflicted outage that repeats every run.
    """
    segments: list[dict[str, Any]] = []
    missing = 0
    for job_id in job_ids:
        try:
            result = client.get(TIMESHEETS_MODULE, f"jobs/{job_id}/timesheets")
        except APIError as exc:
            if exc.status_code != 404:
                raise
            missing += 1
            continue
        for segment in _as_records(result):
            segments.append({**segment, "jobId": segment.get("jobId") or job_id})
    if missing:
        logger.info("financial: %d job(s) had no timesheets endpoint response (404)", missing)
    return segments


def fetch_job_costs(
    client: ServiceTitanClient,
    *,
    today: date,
    window_days: int = FINANCIAL_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Run the built-in Job Costing Summary report over the financial window.

    Raises a :class:`reporting.ReportUnavailableError` subclass — never returns a
    partial or best-guess result. See ``feeds/reporting.py``.
    """
    ref = reporting.find_job_costing_summary(client)
    # Replaying the report's metadata GET before POSTing its data is not
    # redundant: ServiceTitan answers the POST with
    # `Invalid report parameter: [From]` if the report was never described in
    # this token's session. Profit Wizard hit this the hard way on its cached
    # report-id path; the exporter discovers fresh every run, but the ordering
    # is cheap to keep and the failure it prevents is opaque.
    reporting.report_metadata(client, ref)
    parameters = job_cost_parameters(today, window_days=window_days)
    return reporting.fetch_report_rows(client, ref, parameters=parameters)


def job_cost_parameters(
    today: date, *, window_days: int = FINANCIAL_WINDOW_DAYS
) -> list[dict[str, Any]]:
    """The report's ``DateType``/``From``/``To`` parameters for the window.

    Transcribed from Profit Wizard's own call (``sync-job-costs.ts``) rather than
    invented, so the exporter's numbers match what it produces today with no
    reconciliation: ``DateType`` 1, and ``From``/``To`` as bare ``YYYY-MM-DD``.
    ``To`` is today's date, not "now" — the report is date-parameterised, and
    asking for a partial day would make two runs on the same day disagree.
    """
    start = (today - timedelta(days=window_days)).isoformat()
    return [
        {"name": "DateType", "value": 1},
        {"name": "From", "value": start},
        {"name": "To", "value": today.isoformat()},
    ]


def _as_records(result: Any) -> list[dict[str, Any]]:
    """The job-timesheets endpoint answers with a bare array on some tenants and a
    ``{"data": [...]}`` envelope on others — Profit Wizard accepts both, so do we."""
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        return [r for r in result.get("data") or [] if isinstance(r, dict)]
    return []


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")

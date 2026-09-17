"""MCP server exposing ServiceTitan API as structured tools for AI agents.

Run via:  st-mcp          (stdio transport, for Claude Code / Claude Desktop)
          python -m st_cli.mcp_server
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, ParamSpec, TypeVar, cast

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from st_cli.availability import _build_busy_summary
from st_cli.client import ServiceTitanClient
from st_cli.commands.reporting import _report_rows_to_dicts
from st_cli.config import load_settings
from st_cli.dates import apply_date_params, validate_max_range
from st_cli.engine import McpDeps, register_mcp_tools
from st_cli.exceptions import (
    APIError,
    AuthError,
    ConfigError,
    DateParseError,
    NotFoundError,
    RateLimitError,
    STCLIError,
)
from st_cli.pagination import fetch_all, fetch_page
from st_cli.registry import all_modules

# ---------------------------------------------------------------------------
# Lifespan — create/destroy ServiceTitanClient once per server run
# ---------------------------------------------------------------------------

_MAX_RESULTS_CAP = 1000
_DEFAULT_MAX_RESULTS = 200


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    settings = load_settings()
    client = ServiceTitanClient(settings)
    try:
        yield {"client": client}
    finally:
        client.close()


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="ServiceTitan",
    instructions=(
        "ServiceTitan API v2 tools for home-services contractors — full coverage "
        "across all 24 modules: CRM, Jobs/JPM, Dispatch, Pricebook, Inventory, "
        "Sales & Estimates (salestech), Accounting, Memberships, Payroll, Settings, "
        "Marketing (+Ads), Timesheets, Equipment Systems, Findings, Task Management, "
        "Telecom, Customer Interactions, Forms, Reporting, Job Booking, Marketing "
        "Reputation, Scheduling Pro, and Service Agreements. "
        "Tool names follow st_{module}_{resource}_{action} (e.g. st_salestech_estimates_sell, "
        "st_inventory_purchase_orders_approve). Estimates live under salestech; job types "
        "under jpm. "
        "List tools take an optional `filters` dict of query params and support pagination "
        "via `page` (single page) or auto-pagination up to `max_results`. "
        "Export tools (st_{module}_export_{feed}) stream change-feeds; pass `continue_from` "
        "to resume from a prior token. "
        "Date filters accept ISO dates or range strings like 'today', 'last-week', "
        "'this-month', 'last-30-days'. The capacity endpoint has a 14-day maximum range. "
        "Reporting is rate-limited to 1 of the same report per minute per tenant."
    ),
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_client(ctx: Context) -> ServiceTitanClient:
    """Retrieve the ServiceTitanClient from lifespan context."""
    return cast(ServiceTitanClient, ctx.lifespan_context["client"])


def _paginate(
    client: ServiceTitanClient,
    module: str,
    resource: str,
    params: dict[str, Any] | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """Fetch data with smart pagination.

    - If `page` is given, return that single page's data.
    - If `page` is None, auto-paginate up to `max_results` records.

    Returns {"data": [...], "totalCount": int, "truncated": bool}.
    """
    max_results = min(max_results, _MAX_RESULTS_CAP)

    if page is not None:
        envelope = fetch_page(client, module, resource, params, page=page, page_size=page_size)
        return {
            "data": envelope.get("data", []),
            "totalCount": envelope.get("totalCount"),
            "truncated": False,
        }

    collected: list[dict[str, Any]] = []
    truncated = False
    for record in fetch_all(client, module, resource, params, page_size=page_size):
        collected.append(record)
        if len(collected) >= max_results:
            truncated = True
            break

    return {
        "data": collected,
        "totalCount": len(collected),
        "truncated": truncated,
    }


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _handle_errors(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Decorator that maps STCLIError subtypes to ToolError.

    Typed with a ParamSpec so the tool it wraps keeps its exact signature — FastMCP
    introspects that signature for the tool's input schema, and mypy needs it to
    see the tool as typed at all.
    """

    @functools.wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return fn(*args, **kwargs)
        except NotFoundError as exc:
            raise ToolError(f"Not found: {exc.detail}") from exc
        except RateLimitError as exc:
            raise ToolError(f"Rate limited: {exc.detail}") from exc
        except AuthError as exc:
            raise ToolError(f"Authentication error: {exc}") from exc
        except ConfigError as exc:
            raise ToolError(f"Configuration error: {exc}") from exc
        except DateParseError as exc:
            raise ToolError(f"Invalid date range: {exc}") from exc
        except APIError as exc:
            raise ToolError(f"API error (HTTP {exc.status_code}): {exc.detail}") from exc
        except STCLIError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


# ===========================================================================
# CRM Tools
# ===========================================================================


@mcp.tool(tags={"crm", "read"})
@_handle_errors
def st_crm_customers_list(
    ctx: Context,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List customers. Filter by name, email, or phone."""
    params: dict[str, Any] = {}
    if name:
        params["name"] = name
    if email:
        params["email"] = email
    if phone:
        params["phone"] = phone
    return _paginate(_get_client(ctx), "crm", "customers", params, page, page_size, max_results)


@mcp.tool(tags={"crm", "read"})
@_handle_errors
def st_crm_customers_get(ctx: Context, customer_id: int) -> dict[str, Any]:
    """Get a single customer by ID."""
    return cast(dict[str, Any], _get_client(ctx).get("crm", f"customers/{customer_id}"))


@mcp.tool(tags={"crm", "write"})
@_handle_errors
def st_crm_customers_create(ctx: Context, data: dict[str, Any]) -> dict[str, Any]:
    """Create a new customer. `data` must include at least {\"name\": \"...\"}."""
    return cast(dict[str, Any], _get_client(ctx).post("crm", "customers", json_body=data))


@mcp.tool(tags={"crm", "write"})
@_handle_errors
def st_crm_customers_update(ctx: Context, customer_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Update an existing customer. `data` contains fields to change."""
    return cast(
        dict[str, Any], _get_client(ctx).patch("crm", f"customers/{customer_id}", json_body=data)
    )


@mcp.tool(tags={"crm", "read"})
@_handle_errors
def st_crm_locations_list(
    ctx: Context,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List locations."""
    return _paginate(_get_client(ctx), "crm", "locations", None, page, page_size, max_results)


@mcp.tool(tags={"crm", "read"})
@_handle_errors
def st_crm_locations_get(ctx: Context, location_id: int) -> dict[str, Any]:
    """Get a single location by ID."""
    return cast(dict[str, Any], _get_client(ctx).get("crm", f"locations/{location_id}"))


@mcp.tool(tags={"crm", "read"})
@_handle_errors
def st_crm_bookings_list(
    ctx: Context,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List bookings."""
    return _paginate(_get_client(ctx), "crm", "bookings", None, page, page_size, max_results)


@mcp.tool(tags={"crm", "read"})
@_handle_errors
def st_crm_bookings_get(ctx: Context, booking_id: int) -> dict[str, Any]:
    """Get a single booking by ID."""
    return cast(dict[str, Any], _get_client(ctx).get("crm", f"bookings/{booking_id}"))


@mcp.tool(tags={"crm", "write"})
@_handle_errors
def st_crm_bookings_create(ctx: Context, data: dict[str, Any]) -> dict[str, Any]:
    """Create a new booking."""
    return cast(dict[str, Any], _get_client(ctx).post("crm", "bookings", json_body=data))


@mcp.tool(tags={"crm", "write"})
@_handle_errors
def st_crm_bookings_update(ctx: Context, booking_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Update an existing booking."""
    return cast(
        dict[str, Any], _get_client(ctx).patch("crm", f"bookings/{booking_id}", json_body=data)
    )


@mcp.tool(tags={"crm", "read"})
@_handle_errors
def st_crm_contacts_list(
    ctx: Context,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List contacts."""
    return _paginate(_get_client(ctx), "crm", "contacts", None, page, page_size, max_results)


@mcp.tool(tags={"crm", "read"})
@_handle_errors
def st_crm_contacts_get(ctx: Context, contact_id: int) -> dict[str, Any]:
    """Get a single contact by ID."""
    return cast(dict[str, Any], _get_client(ctx).get("crm", f"contacts/{contact_id}"))


# ===========================================================================
# Jobs / Project Management Tools
# ===========================================================================


@mcp.tool(tags={"jpm", "read"})
@_handle_errors
def st_jpm_jobs_list(
    ctx: Context,
    status: str | None = None,
    customer_id: int | None = None,
    date_range: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    sort: str | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List jobs. Filter by status, customer_id, or date range. Optional `sort` —
    the endpoint accepts only Id, ModifiedOn, CreatedOn and Priority, so e.g.
    `-Id` (newest first) or `+createdOn`; `-completedOn` is rejected with a 400."""
    params: dict[str, Any] = {}
    if status:
        params["jobStatus"] = status
    if customer_id:
        params["customerId"] = customer_id
    if sort:
        params["sort"] = sort
    apply_date_params(params, date_range, created_after, created_before)
    return _paginate(_get_client(ctx), "jpm", "jobs", params, page, page_size, max_results)


@mcp.tool(tags={"jpm", "read"})
@_handle_errors
def st_jpm_jobs_get(ctx: Context, job_id: int) -> dict[str, Any]:
    """Get a single job by ID."""
    return cast(dict[str, Any], _get_client(ctx).get("jpm", f"jobs/{job_id}"))


@mcp.tool(tags={"jpm", "write"})
@_handle_errors
def st_jpm_jobs_create(ctx: Context, data: dict[str, Any]) -> dict[str, Any]:
    """Create a new job."""
    return cast(dict[str, Any], _get_client(ctx).post("jpm", "jobs", json_body=data))


@mcp.tool(tags={"jpm", "write"})
@_handle_errors
def st_jpm_jobs_update(ctx: Context, job_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Update an existing job."""
    return cast(dict[str, Any], _get_client(ctx).patch("jpm", f"jobs/{job_id}", json_body=data))


@mcp.tool(tags={"jpm", "write"})
@_handle_errors
def st_jpm_jobs_cancel(ctx: Context, job_id: int) -> dict[str, Any]:
    """Cancel a job."""
    _get_client(ctx).post("jpm", f"jobs/{job_id}/cancel")
    return {"status": "cancelled", "jobId": job_id}


@mcp.tool(tags={"jpm", "read"})
@_handle_errors
def st_jpm_appointments_list(
    ctx: Context,
    job_id: int | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List appointments. Optionally filter by job_id."""
    params: dict[str, Any] = {}
    if job_id:
        params["jobId"] = job_id
    return _paginate(_get_client(ctx), "jpm", "appointments", params, page, page_size, max_results)


@mcp.tool(tags={"jpm", "read"})
@_handle_errors
def st_jpm_appointments_get(ctx: Context, appointment_id: int) -> dict[str, Any]:
    """Get a single appointment by ID."""
    return cast(dict[str, Any], _get_client(ctx).get("jpm", f"appointments/{appointment_id}"))


@mcp.tool(tags={"jpm", "read"})
@_handle_errors
def st_jpm_projects_list(
    ctx: Context,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List projects."""
    return _paginate(_get_client(ctx), "jpm", "projects", None, page, page_size, max_results)


@mcp.tool(tags={"jpm", "read"})
@_handle_errors
def st_jpm_projects_get(ctx: Context, project_id: int) -> dict[str, Any]:
    """Get a single project by ID."""
    return cast(dict[str, Any], _get_client(ctx).get("jpm", f"projects/{project_id}"))


# ===========================================================================
# Dispatch Tools
# ===========================================================================


@mcp.tool(tags={"dispatch", "read"})
@_handle_errors
def st_dispatch_shifts_list(
    ctx: Context,
    date_range: str | None = None,
    starts_after: str | None = None,
    starts_before: str | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List technician shifts. Filter by date range."""
    params: dict[str, Any] = {}
    apply_date_params(
        params,
        date_range,
        starts_after,
        starts_before,
        start_key="startsOnOrAfter",
        end_key="startsBefore",
    )
    return _paginate(
        _get_client(ctx), "dispatch", "technician-shifts", params, page, page_size, max_results
    )


@mcp.tool(tags={"dispatch", "read"})
@_handle_errors
def st_dispatch_assignments_list(
    ctx: Context,
    technician_id: int | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List appointment assignments (technician-to-appointment mapping)."""
    params: dict[str, Any] = {}
    if technician_id:
        params["technicianId"] = technician_id
    return _paginate(
        _get_client(ctx),
        "dispatch",
        "appointment-assignments",
        params,
        page,
        page_size,
        max_results,
    )


@mcp.tool(tags={"dispatch", "read"})
@_handle_errors
def st_dispatch_who_busy(
    ctx: Context,
    date_range: str = "today",
    starts_after: str | None = None,
    starts_before: str | None = None,
) -> dict[str, Any]:
    """Show technician availability: who's busy, who's free.

    Cross-references shifts with booked appointments to compute
    per-technician availability for the given date range.
    """
    client = _get_client(ctx)

    date_params: dict[str, Any] = {}
    apply_date_params(
        date_params,
        date_range,
        starts_after,
        starts_before,
        start_key="startsOnOrAfter",
        end_key="startsBefore",
    )

    shifts = list(fetch_all(client, "dispatch", "technician-shifts", date_params, page_size=200))
    appointments = list(fetch_all(client, "jpm", "appointments", dict(date_params), page_size=200))

    rows = _build_busy_summary(shifts, appointments)
    return {"data": rows, "totalCount": len(rows)}


@mcp.tool(tags={"dispatch", "read"})
@_handle_errors
def st_dispatch_events_list(
    ctx: Context,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List non-job appointments (events)."""
    return _paginate(
        _get_client(ctx), "dispatch", "non-job-appointments", None, page, page_size, max_results
    )


@mcp.tool(tags={"dispatch", "write"})
@_handle_errors
def st_dispatch_events_create(ctx: Context, data: dict[str, Any]) -> dict[str, Any]:
    """Create a non-job appointment (event)."""
    return cast(
        dict[str, Any], _get_client(ctx).post("dispatch", "non-job-appointments", json_body=data)
    )


@mcp.tool(tags={"dispatch", "read"})
@_handle_errors
def st_dispatch_zones_list(
    ctx: Context,
    active: bool | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List dispatch zones. Optionally filter by active status."""
    params: dict[str, Any] = {}
    if active is not None:
        params["active"] = active
    return _paginate(_get_client(ctx), "dispatch", "zones", params, page, page_size, max_results)


@mcp.tool(tags={"dispatch", "read"})
@_handle_errors
def st_dispatch_teams_list(
    ctx: Context,
    active: bool | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List dispatch teams. Optionally filter by active status."""
    params: dict[str, Any] = {}
    if active is not None:
        params["active"] = active
    return _paginate(_get_client(ctx), "dispatch", "teams", params, page, page_size, max_results)


_CAPACITY_MAX_DAYS = 14


@mcp.tool(tags={"dispatch", "read"})
@_handle_errors
def st_dispatch_capacity(
    ctx: Context,
    date_range: str = "this-week",
    starts_after: str | None = None,
    ends_on_or_before: str | None = None,
    business_unit_ids: list[int] | None = None,
    job_type_id: int | None = None,
    skill_based_availability: bool = False,
) -> dict[str, Any]:
    """Query capacity planning data (max 14-day window).

    Calls POST /dispatch/v2/tenant/{tenant}/capacity. The body matches
    Dispatch.V2.CapacityRequestArgs: business_unit_ids is an optional
    flat array, job_type_id is a single optional ID used to size the
    duration of each availability slot.

    Returns the raw response: ``{timeStamp, availabilities[]}`` where
    each availability has start/end, totalAvailability, openAvailability,
    technicians, isAvailable, and isExceedingIdealBookingPercentage.
    """
    client = _get_client(ctx)

    date_params: dict[str, Any] = {}
    apply_date_params(
        date_params,
        date_range,
        starts_after,
        ends_on_or_before,
        start_key="startsOnOrAfter",
        end_key="endsOnOrBefore",
    )
    validate_max_range(date_params, "startsOnOrAfter", "endsOnOrBefore", _CAPACITY_MAX_DAYS)

    body: dict[str, Any] = {
        **date_params,
        "skillBasedAvailability": skill_based_availability,
    }
    if business_unit_ids:
        body["businessUnitIds"] = business_unit_ids
    if job_type_id is not None:
        body["jobTypeId"] = job_type_id

    result = client.post("dispatch", "capacity", json_body=body)
    if isinstance(result, dict):
        return result
    return {"availabilities": result if isinstance(result, list) else []}


# ===========================================================================
# Accounting Tools
# ===========================================================================


@mcp.tool(tags={"accounting", "read"})
@_handle_errors
def st_accounting_invoices_list(
    ctx: Context,
    status: str | None = None,
    date_range: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List invoices. Filter by status or date range."""
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    apply_date_params(params, date_range, created_after, created_before)
    return _paginate(
        _get_client(ctx), "accounting", "invoices", params, page, page_size, max_results
    )


@mcp.tool(tags={"accounting", "read"})
@_handle_errors
def st_accounting_invoices_get(ctx: Context, invoice_id: int) -> dict[str, Any]:
    """Get a single invoice by ID."""
    return cast(dict[str, Any], _get_client(ctx).get("accounting", f"invoices/{invoice_id}"))


@mcp.tool(tags={"accounting", "read"})
@_handle_errors
def st_accounting_payments_list(
    ctx: Context,
    date_range: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List payments. Filter by date range."""
    params: dict[str, Any] = {}
    apply_date_params(params, date_range, created_after, created_before)
    return _paginate(
        _get_client(ctx), "accounting", "payments", params, page, page_size, max_results
    )


# ===========================================================================
# Memberships Tools
# ===========================================================================


@mcp.tool(tags={"memberships", "read"})
@_handle_errors
def st_memberships_list(
    ctx: Context,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List memberships."""
    return _paginate(
        _get_client(ctx), "memberships", "memberships", None, page, page_size, max_results
    )


@mcp.tool(tags={"memberships", "read"})
@_handle_errors
def st_memberships_get(ctx: Context, membership_id: int) -> dict[str, Any]:
    """Get a single membership by ID."""
    return cast(dict[str, Any], _get_client(ctx).get("memberships", f"memberships/{membership_id}"))


@mcp.tool(tags={"memberships", "read"})
@_handle_errors
def st_memberships_types_list(
    ctx: Context,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List membership types."""
    return _paginate(
        _get_client(ctx), "memberships", "membership-types", None, page, page_size, max_results
    )


# ===========================================================================
# Reporting Tools
# ===========================================================================


@mcp.tool(tags={"reporting", "read"})
@_handle_errors
def st_reporting_categories_list(
    ctx: Context,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List report categories available for this tenant."""
    return _paginate(
        _get_client(ctx), "reporting", "report-categories", None, page, page_size, max_results
    )


@mcp.tool(tags={"reporting", "read"})
@_handle_errors
def st_reporting_reports_list(
    ctx: Context,
    category_id: str,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List reports in a category. Call st_reporting_categories_list first."""
    return _paginate(
        _get_client(ctx),
        "reporting",
        f"report-category/{category_id}/reports",
        None,
        page,
        page_size,
        max_results,
    )


@mcp.tool(tags={"reporting", "read"})
@_handle_errors
def st_reporting_report_fields(
    ctx: Context,
    category_id: str,
    report_id: str,
) -> dict[str, Any]:
    """Get a report's metadata: available fields and required parameters.

    Call this before st_reporting_report_data to discover what parameters
    (e.g. From, To dates) the report requires.
    """
    return cast(
        dict[str, Any],
        _get_client(ctx).get("reporting", f"report-category/{category_id}/reports/{report_id}"),
    )


@mcp.tool(tags={"reporting", "read"})
@_handle_errors
def st_reporting_report_data(
    ctx: Context,
    category_id: str,
    report_id: str,
    from_date: str | None = None,
    to_date: str | None = None,
    extra_parameters: list[dict[str, str]] | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """Fetch report data (e.g. Technician Scorecard).

    Most reports require From/To date parameters.
    Use st_reporting_report_fields first to discover required parameters.

    Returns rows as dicts keyed by field name, plus fields metadata.
    Rate limit: 1 of the same report per minute per tenant.
    """
    client = _get_client(ctx)
    max_results = min(max_results, _MAX_RESULTS_CAP)

    parameters: list[dict[str, str]] = []
    if from_date:
        parameters.append({"name": "From", "value": from_date})
    if to_date:
        parameters.append({"name": "To", "value": to_date})
    if extra_parameters:
        parameters.extend(extra_parameters)

    body: dict[str, Any] = {"parameters": parameters}
    resource = f"report-category/{category_id}/reports/{report_id}/data"

    if page is not None:
        resp = client.post(
            "reporting",
            resource,
            json_body=body,
            params={"page": page, "pageSize": page_size},
        )
        return {
            "fields": resp.get("fields", []),
            "data": _report_rows_to_dicts(resp.get("fields", []), resp.get("data", [])),
            "totalCount": resp.get("totalCount"),
            "truncated": False,
        }

    # Auto-paginate
    all_fields: list[dict[str, Any]] = []
    all_rows: list[list[Any]] = []
    current_page = 1
    truncated = False
    while True:
        resp = client.post(
            "reporting",
            resource,
            json_body=body,
            params={"page": current_page, "pageSize": page_size},
        )
        if not all_fields:
            all_fields = resp.get("fields", [])
        all_rows.extend(resp.get("data", []))
        if len(all_rows) >= max_results:
            all_rows = all_rows[:max_results]
            truncated = True
            break
        if not resp.get("hasMore", False):
            break
        current_page += 1

    return {
        "fields": all_fields,
        "data": _report_rows_to_dicts(all_fields, all_rows),
        "totalCount": len(all_rows),
        "truncated": truncated,
    }


# ===========================================================================
# Settings Tools
# ===========================================================================


@mcp.tool(tags={"settings", "read"})
@_handle_errors
def st_settings_business_units_list(
    ctx: Context,
    active: bool | None = None,
    page: int | None = None,
    page_size: int = 50,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List business units. Optionally filter by active status."""
    params: dict[str, Any] = {}
    if active is not None:
        params["active"] = active
    return _paginate(
        _get_client(ctx), "settings", "business-units", params, page, page_size, max_results
    )


@mcp.tool(tags={"settings", "read"})
@_handle_errors
def st_settings_business_units_get(ctx: Context, business_unit_id: int) -> dict[str, Any]:
    """Get a single business unit by ID."""
    return cast(
        dict[str, Any], _get_client(ctx).get("settings", f"business-units/{business_unit_id}")
    )


# ===========================================================================
# Generated tools — the rest of the ServiceTitan surface, from the registry.
# These cover every module/resource not hand-wrapped above (job types now live
# under jpm, estimates under salestech — fixing the two prior routing bugs).
# ===========================================================================

_deps = McpDeps(
    get_client=_get_client,
    paginate=_paginate,
    handle_errors=_handle_errors,
    max_results_cap=_MAX_RESULTS_CAP,
    default_max_results=_DEFAULT_MAX_RESULTS,
)

for _module in all_modules():
    register_mcp_tools(mcp, _module, _deps)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

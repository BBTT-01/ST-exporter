"""Dispatch commands: shifts, who-busy, assignments, events, capacity, zones, teams."""

from __future__ import annotations

import json
from typing import Any, Optional

import typer

from st_cli.availability import _build_busy_summary
from st_cli.dates import apply_date_params, validate_max_range
from st_cli.output import Column, render, render_single
from st_cli.pagination import fetch_all, fetch_page

MODULE = "dispatch"
_CAPACITY_MAX_DAYS = 14

app = typer.Typer(help="Dispatch — shifts, availability, assignments, events, capacity")

SHIFT_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Technician ID", "technicianId"),
    ("Technician Name", "technicianName"),
    ("Start", "start"),
    ("End", "end"),
    ("Active", "active"),
]

ASSIGNMENT_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Technician ID", "technicianId"),
    ("Technician Name", "technicianName"),
    ("Appointment ID", "appointmentId"),
    ("Job ID", "jobId"),
    ("Status", "status"),
    ("Assigned On", "assignedOn"),
]

EVENT_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Name", "name"),
    ("Start", "start"),
    ("End", "end"),
    ("Technician ID", "technicianId"),
]


# --- Technician Shifts ---


@app.command("shifts-list")
def shifts_list(
    ctx: typer.Context,
    range_val: Optional[str] = typer.Option(None, "--range", help="Date range (e.g. last-week)"),
    from_date: Optional[str] = typer.Option(
        None, "--from-date", help="Starts on or after (YYYY-MM-DD)"
    ),
    to_date: Optional[str] = typer.Option(None, "--to-date", help="Starts before (YYYY-MM-DD)"),
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List technician shifts."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    params: dict[str, Any] = {}
    apply_date_params(
        params,
        range_val,
        from_date,
        to_date,
        start_key="startsOnOrAfter",
        end_key="startsBefore",
    )
    if all_pages:
        data = list(fetch_all(client, MODULE, "technician-shifts", params, page_size=page_size))
        render(data, SHIFT_COLUMNS, as_json=as_json, title="Technician Shifts")
    else:
        envelope = fetch_page(
            client, MODULE, "technician-shifts", params, page=page, page_size=page_size
        )
        render(
            envelope.get("data", []),
            SHIFT_COLUMNS,
            as_json=as_json,
            title="Technician Shifts",
            total_count=envelope.get("totalCount"),
        )


# --- Appointment Assignments ---


@app.command("assignments-list")
def assignments_list(
    ctx: typer.Context,
    technician_id: Optional[int] = typer.Option(None, help="Filter by technician ID"),
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List appointment assignments (technician → appointment mapping)."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    params: dict[str, Any] = {}
    if technician_id:
        params["technicianId"] = technician_id
    if all_pages:
        data = list(
            fetch_all(client, MODULE, "appointment-assignments", params, page_size=page_size)
        )
        render(data, ASSIGNMENT_COLUMNS, as_json=as_json, title="Appointment Assignments")
    else:
        envelope = fetch_page(
            client, MODULE, "appointment-assignments", params, page=page, page_size=page_size
        )
        render(
            envelope.get("data", []),
            ASSIGNMENT_COLUMNS,
            as_json=as_json,
            title="Appointment Assignments",
            total_count=envelope.get("totalCount"),
        )


# --- Who's Busy ---


WHO_BUSY_COLUMNS: list[Column] = [
    ("Tech ID", "technicianId"),
    ("Name", "technicianName"),
    ("Shifts", "shifts"),
    ("Shift Hrs", "shiftHours"),
    ("Appts", "appointments"),
    ("Booked Hrs", "bookedHours"),
    ("Avail Hrs", "availableHours"),
    ("% Booked", "percentBooked"),
]


@app.command("who-busy")
def who_busy(
    ctx: typer.Context,
    range_val: Optional[str] = typer.Option("today", "--range", help="Date range (default: today)"),
    from_date: Optional[str] = typer.Option(
        None, "--from-date", help="Starts on or after (YYYY-MM-DD)"
    ),
    to_date: Optional[str] = typer.Option(None, "--to-date", help="Starts before (YYYY-MM-DD)"),
) -> None:
    """Show technician availability: who's busy, who's free.

    Cross-references technician shifts with booked appointments to compute
    availability per technician for the given date range.
    """
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]

    # Both shifts and appointments use the same startsOnOrAfter/startsBefore keys
    date_params: dict[str, Any] = {}
    apply_date_params(
        date_params,
        range_val,
        from_date,
        to_date,
        start_key="startsOnOrAfter",
        end_key="startsBefore",
    )

    shifts = list(fetch_all(client, MODULE, "technician-shifts", date_params, page_size=200))
    appointments = list(fetch_all(client, "jpm", "appointments", dict(date_params), page_size=200))

    rows = _build_busy_summary(shifts, appointments)

    if not rows:
        typer.echo("No technician shifts found for the given range.")
        return

    render(rows, WHO_BUSY_COLUMNS, as_json=as_json, title="Technician Availability")


# --- Non-Job Events ---


@app.command("events-list")
def events_list(
    ctx: typer.Context,
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
) -> None:
    """List non-job appointments (events)."""
    client = ctx.obj["client"]
    envelope = fetch_page(client, MODULE, "non-job-appointments", page=page, page_size=page_size)
    render(
        envelope.get("data", []),
        EVENT_COLUMNS,
        as_json=ctx.obj["json"],
        title="Events",
        total_count=envelope.get("totalCount"),
    )


@app.command("events-create")
def events_create(
    ctx: typer.Context,
    data: str = typer.Option(..., "--data", help="Event data as JSON"),
) -> None:
    """Create a non-job appointment (event)."""
    client = ctx.obj["client"]
    body = json.loads(data)
    result = client.post(MODULE, "non-job-appointments", json_body=body)
    render_single(result, EVENT_COLUMNS, as_json=ctx.obj["json"])


# --- Dispatch Zones ---


ZONE_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Name", "name"),
    ("Active", "active"),
]


@app.command("zones-list")
def zones_list(
    ctx: typer.Context,
    active: Optional[bool] = typer.Option(None, help="Filter by active status"),
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List dispatch zones."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    params: dict[str, Any] = {}
    if active is not None:
        params["active"] = active
    if all_pages:
        data = list(fetch_all(client, MODULE, "zones", params, page_size=page_size))
        render(data, ZONE_COLUMNS, as_json=as_json, title="Dispatch Zones")
    else:
        envelope = fetch_page(client, MODULE, "zones", params, page=page, page_size=page_size)
        render(
            envelope.get("data", []),
            ZONE_COLUMNS,
            as_json=as_json,
            title="Dispatch Zones",
            total_count=envelope.get("totalCount"),
        )


# --- Dispatch Teams ---


TEAM_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Name", "name"),
    ("Active", "active"),
]


@app.command("teams-list")
def teams_list(
    ctx: typer.Context,
    active: Optional[bool] = typer.Option(None, help="Filter by active status"),
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List dispatch teams."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    params: dict[str, Any] = {}
    if active is not None:
        params["active"] = active
    if all_pages:
        data = list(fetch_all(client, MODULE, "teams", params, page_size=page_size))
        render(data, TEAM_COLUMNS, as_json=as_json, title="Dispatch Teams")
    else:
        envelope = fetch_page(client, MODULE, "teams", params, page=page, page_size=page_size)
        render(
            envelope.get("data", []),
            TEAM_COLUMNS,
            as_json=as_json,
            title="Dispatch Teams",
            total_count=envelope.get("totalCount"),
        )


# --- Capacity ---


def _parse_id_list(value: str | None) -> list[int] | None:
    """Parse a comma-separated string of IDs into a list of ints."""
    if not value:
        return None
    return [int(x.strip()) for x in value.split(",") if x.strip()]


CAPACITY_COLUMNS: list[Column] = [
    ("Start", "start"),
    ("End", "end"),
    ("Business Units", "businessUnitIds"),
    ("Total", "totalAvailability"),
    ("Open", "openAvailability"),
    ("Available", "isAvailable"),
    ("Over Ideal %", "isExceedingIdealBookingPercentage"),
]


@app.command("capacity")
def capacity(
    ctx: typer.Context,
    range_val: Optional[str] = typer.Option(
        "this-week", "--range", help="Date range (max 14 days, e.g. this-week)"
    ),
    from_date: Optional[str] = typer.Option(
        None, "--from-date", help="Starts on or after (YYYY-MM-DD)"
    ),
    to_date: Optional[str] = typer.Option(None, "--to-date", help="Ends on or before (YYYY-MM-DD)"),
    business_unit_ids: Optional[str] = typer.Option(
        None, "--bu-ids", help="Comma-separated business unit IDs"
    ),
    job_type_id: Optional[int] = typer.Option(
        None, "--job-type-id", help="Job type ID for duration-based availability"
    ),
    skill_based: bool = typer.Option(
        False, "--skill-based", help="Enable skill-based availability"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Query capacity planning data (max 14-day window).

    Calls POST /dispatch/v2/tenant/{tenant}/capacity per the
    Dispatch.V2.CapacityRequestArgs schema. Returns availability time
    frames with total/open hours, technicians, and ideal-booking flags.
    """
    client = ctx.obj["client"]
    as_json = ctx.obj["json"] or json_output

    date_params: dict[str, Any] = {}
    apply_date_params(
        date_params,
        range_val,
        from_date,
        to_date,
        start_key="startsOnOrAfter",
        end_key="endsOnOrBefore",
    )
    validate_max_range(date_params, "startsOnOrAfter", "endsOnOrBefore", _CAPACITY_MAX_DAYS)

    body: dict[str, Any] = {
        **date_params,
        "skillBasedAvailability": skill_based,
    }
    bu_ids = _parse_id_list(business_unit_ids)
    if bu_ids:
        body["businessUnitIds"] = bu_ids
    if job_type_id is not None:
        body["jobTypeId"] = job_type_id

    result = client.post(MODULE, "capacity", json_body=body)

    if as_json:
        render(result, CAPACITY_COLUMNS, as_json=True, title="Capacity")
        return

    availabilities = (result or {}).get("availabilities", []) if isinstance(result, dict) else []
    if not availabilities:
        typer.echo("No availability returned for the given range.")
        return
    render(availabilities, CAPACITY_COLUMNS, as_json=False, title="Capacity")

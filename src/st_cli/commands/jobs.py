"""Jobs commands: jobs, appointments, projects."""

from __future__ import annotations

import json
from typing import Optional

import typer

from st_cli.dates import apply_date_params
from st_cli.output import Column, render, render_single
from st_cli.pagination import fetch_all, fetch_page

MODULE = "jpm"

app = typer.Typer(help="Jobs — jobs, appointments, projects")

JOB_COLUMNS: list[Column] = [
    ("ID", "id"),
    # ServiceTitan's JPM job object names this `jobNumber`; reading only `number`
    # showed a permanently blank Number column on every `st jobs list` ever run.
    # The exporter hit the identical bug (see `denormalize.build_job_rows`).
    # Widen-only: `number` stays as a fallback, so nothing that worked stops.
    ("Number", "jobNumber|number"),
    ("Customer ID", "customerId"),
    ("Status", "jobStatus"),
    ("Type", "jobTypeName"),
    ("Total", "total"),
    ("Created", "createdOn"),
]

APPOINTMENT_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Job ID", "jobId"),
    ("Status", "status"),
    ("Start", "start"),
    ("End", "end"),
    ("Arrival Window Start", "arrivalWindowStart"),
    ("Arrival Window End", "arrivalWindowEnd"),
]

PROJECT_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Number", "number"),
    ("Name", "name"),
    ("Status", "status"),
    ("Customer ID", "customerId"),
]


@app.command("list")
def jobs_list(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, help="Filter by job status"),
    range_val: Optional[str] = typer.Option(
        None, "--range", help="Date range (e.g. last-week, this-month)"
    ),
    from_date: Optional[str] = typer.Option(
        None, "--from-date", help="Created on or after (YYYY-MM-DD)"
    ),
    to_date: Optional[str] = typer.Option(None, "--to-date", help="Created before (YYYY-MM-DD)"),
    customer_id: Optional[int] = typer.Option(None, help="Filter by customer ID"),
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List jobs."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    params: dict = {}
    if status:
        params["jobStatus"] = status
    if customer_id:
        params["customerId"] = customer_id
    apply_date_params(params, range_val, from_date, to_date)

    if all_pages:
        data = list(fetch_all(client, MODULE, "jobs", params, page_size=page_size))
        render(data, JOB_COLUMNS, as_json=as_json, title="Jobs")
    else:
        envelope = fetch_page(client, MODULE, "jobs", params, page=page, page_size=page_size)
        render(
            envelope.get("data", []),
            JOB_COLUMNS,
            as_json=as_json,
            title="Jobs",
            total_count=envelope.get("totalCount"),
        )


@app.command("get")
def jobs_get(ctx: typer.Context, job_id: int = typer.Argument(..., help="Job ID")) -> None:
    """Get a single job by ID."""
    client = ctx.obj["client"]
    data = client.get(MODULE, f"jobs/{job_id}")
    render_single(data, JOB_COLUMNS, as_json=ctx.obj["json"])


@app.command("create")
def jobs_create(
    ctx: typer.Context,
    data: str = typer.Option(..., "--data", help="Job data as JSON"),
) -> None:
    """Create a new job."""
    client = ctx.obj["client"]
    body = json.loads(data)
    result = client.post(MODULE, "jobs", json_body=body)
    render_single(result, JOB_COLUMNS, as_json=ctx.obj["json"])


@app.command("update")
def jobs_update(
    ctx: typer.Context,
    job_id: int = typer.Argument(..., help="Job ID"),
    data: str = typer.Option(..., "--data", help="Fields to update as JSON"),
) -> None:
    """Update an existing job."""
    client = ctx.obj["client"]
    body = json.loads(data)
    result = client.patch(MODULE, f"jobs/{job_id}", json_body=body)
    render_single(result, JOB_COLUMNS, as_json=ctx.obj["json"])


@app.command("cancel")
def jobs_cancel(ctx: typer.Context, job_id: int = typer.Argument(..., help="Job ID")) -> None:
    """Cancel a job."""
    client = ctx.obj["client"]
    client.post(MODULE, f"jobs/{job_id}/cancel")
    typer.echo(f"Job {job_id} cancelled.")


@app.command("appointments-list")
def appointments_list(
    ctx: typer.Context,
    job_id: Optional[int] = typer.Option(None, help="Filter by job ID"),
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
) -> None:
    """List appointments."""
    client = ctx.obj["client"]
    params: dict = {}
    if job_id:
        params["jobId"] = job_id
    envelope = fetch_page(client, MODULE, "appointments", params, page=page, page_size=page_size)
    render(
        envelope.get("data", []),
        APPOINTMENT_COLUMNS,
        as_json=ctx.obj["json"],
        title="Appointments",
        total_count=envelope.get("totalCount"),
    )


@app.command("appointments-get")
def appointments_get(
    ctx: typer.Context, appointment_id: int = typer.Argument(..., help="Appointment ID")
) -> None:
    """Get a single appointment by ID."""
    client = ctx.obj["client"]
    data = client.get(MODULE, f"appointments/{appointment_id}")
    render_single(data, APPOINTMENT_COLUMNS, as_json=ctx.obj["json"])


@app.command("projects-list")
def projects_list(
    ctx: typer.Context,
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
) -> None:
    """List projects."""
    client = ctx.obj["client"]
    envelope = fetch_page(client, MODULE, "projects", page=page, page_size=page_size)
    render(
        envelope.get("data", []),
        PROJECT_COLUMNS,
        as_json=ctx.obj["json"],
        title="Projects",
        total_count=envelope.get("totalCount"),
    )


@app.command("projects-get")
def projects_get(
    ctx: typer.Context, project_id: int = typer.Argument(..., help="Project ID")
) -> None:
    """Get a single project by ID."""
    client = ctx.obj["client"]
    data = client.get(MODULE, f"projects/{project_id}")
    render_single(data, PROJECT_COLUMNS, as_json=ctx.obj["json"])

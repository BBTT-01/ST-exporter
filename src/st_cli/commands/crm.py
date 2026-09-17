"""CRM commands: customers, locations, bookings, contacts."""

from __future__ import annotations

import json
from typing import Any, Optional

import typer

from st_cli.output import Column, render, render_single
from st_cli.pagination import fetch_all, fetch_page

MODULE = "crm"

app = typer.Typer(help="CRM — customers, locations, bookings, contacts")

CUSTOMER_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Name", "name"),
    # ServiceTitan returns a customer's contact details as `emailSettings[]` /
    # `phoneSettings[]`; the flat scalars are the fallback, not the source —
    # Profit Wizard's production client reads them the same way round
    # (`lib/crm/servicetitan.ts:903-906`). Reading only the scalar leaves the
    # column blank on every row, which looks like a customer with no phone.
    # Every plausible spelling, widest first: ServiceTitan's documented
    # `CustomerPhoneSettings` element looks like `{phoneNumber, doNotText}`, but
    # `phone` is what the fixtures (and the previous reading of the docs) say —
    # so BOTH are read, plus the flat scalar. Unverified against a live tenant;
    # tracked in KNOWN_UNVERIFIED.md. Narrowing this to one spelling is how
    # `job_number` stayed blank for the life of that feature.
    ("Email", "emailSettings.0.email|emailSettings.0.emailAddress|email"),
    ("Phone", "phoneSettings.0.phoneNumber|phoneSettings.0.phone|phone"),
    ("Active", "active"),
    ("Created", "createdOn"),
]

LOCATION_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Name", "name"),
    ("Address", "address.street"),
    ("City", "address.city"),
    ("State", "address.state"),
    ("Zip", "address.zip"),
]

BOOKING_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Customer ID", "customerId"),
    ("Status", "status"),
    ("Start", "start"),
    ("Created", "createdOn"),
]

CONTACT_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Type", "type"),
    ("Value", "value"),
    ("Customer ID", "customerId"),
]


# --- Customers ---


@app.command()
def customers_list(
    ctx: typer.Context,
    name: Optional[str] = typer.Option(None, help="Filter by customer name"),
    email: Optional[str] = typer.Option(None, help="Filter by email"),
    phone: Optional[str] = typer.Option(None, help="Filter by phone"),
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List customers."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    params: dict[str, Any] = {}
    if name:
        params["name"] = name
    if email:
        params["email"] = email
    if phone:
        params["phone"] = phone

    if all_pages:
        data = list(fetch_all(client, MODULE, "customers", params, page_size=page_size))
        render(data, CUSTOMER_COLUMNS, as_json=as_json, title="Customers")
    else:
        envelope = fetch_page(client, MODULE, "customers", params, page=page, page_size=page_size)
        render(
            envelope.get("data", []),
            CUSTOMER_COLUMNS,
            as_json=as_json,
            title="Customers",
            total_count=envelope.get("totalCount"),
        )


@app.command()
def customers_get(
    ctx: typer.Context, customer_id: int = typer.Argument(..., help="Customer ID")
) -> None:
    """Get a single customer by ID."""
    client = ctx.obj["client"]
    data = client.get(MODULE, f"customers/{customer_id}")
    render_single(data, CUSTOMER_COLUMNS, as_json=ctx.obj["json"])


@app.command()
def customers_create(
    ctx: typer.Context,
    name: str = typer.Option(..., help="Customer name"),
    data: Optional[str] = typer.Option(None, "--data", help="Additional fields as JSON"),
) -> None:
    """Create a new customer."""
    client = ctx.obj["client"]
    body: dict[str, Any] = {"name": name}
    if data:
        body.update(json.loads(data))
    result = client.post(MODULE, "customers", json_body=body)
    render_single(result, CUSTOMER_COLUMNS, as_json=ctx.obj["json"])


@app.command()
def customers_update(
    ctx: typer.Context,
    customer_id: int = typer.Argument(..., help="Customer ID"),
    name: Optional[str] = typer.Option(None, help="Customer name"),
    data: Optional[str] = typer.Option(None, "--data", help="Fields to update as JSON"),
) -> None:
    """Update an existing customer."""
    client = ctx.obj["client"]
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if data:
        body.update(json.loads(data))
    result = client.patch(MODULE, f"customers/{customer_id}", json_body=body)
    render_single(result, CUSTOMER_COLUMNS, as_json=ctx.obj["json"])


# --- Locations ---


@app.command()
def locations_list(
    ctx: typer.Context,
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List locations."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    if all_pages:
        data = list(fetch_all(client, MODULE, "locations", page_size=page_size))
        render(data, LOCATION_COLUMNS, as_json=as_json, title="Locations")
    else:
        envelope = fetch_page(client, MODULE, "locations", page=page, page_size=page_size)
        render(
            envelope.get("data", []),
            LOCATION_COLUMNS,
            as_json=as_json,
            title="Locations",
            total_count=envelope.get("totalCount"),
        )


@app.command()
def locations_get(
    ctx: typer.Context, location_id: int = typer.Argument(..., help="Location ID")
) -> None:
    """Get a single location by ID."""
    client = ctx.obj["client"]
    data = client.get(MODULE, f"locations/{location_id}")
    render_single(data, LOCATION_COLUMNS, as_json=ctx.obj["json"])


# --- Bookings ---


@app.command()
def bookings_list(
    ctx: typer.Context,
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List bookings."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    if all_pages:
        data = list(fetch_all(client, MODULE, "bookings", params=None, page_size=page_size))
        render(data, BOOKING_COLUMNS, as_json=as_json, title="Bookings")
    else:
        envelope = fetch_page(client, MODULE, "bookings", page=page, page_size=page_size)
        render(
            envelope.get("data", []),
            BOOKING_COLUMNS,
            as_json=as_json,
            title="Bookings",
            total_count=envelope.get("totalCount"),
        )


@app.command()
def bookings_get(
    ctx: typer.Context, booking_id: int = typer.Argument(..., help="Booking ID")
) -> None:
    """Get a single booking by ID."""
    client = ctx.obj["client"]
    data = client.get(MODULE, f"bookings/{booking_id}")
    render_single(data, BOOKING_COLUMNS, as_json=ctx.obj["json"])


@app.command()
def bookings_create(
    ctx: typer.Context,
    data: str = typer.Option(..., "--data", help="Booking data as JSON"),
) -> None:
    """Create a new booking."""
    client = ctx.obj["client"]
    body = json.loads(data)
    result = client.post(MODULE, "bookings", json_body=body)
    render_single(result, BOOKING_COLUMNS, as_json=ctx.obj["json"])


@app.command()
def bookings_update(
    ctx: typer.Context,
    booking_id: int = typer.Argument(..., help="Booking ID"),
    data: str = typer.Option(..., "--data", help="Fields to update as JSON"),
) -> None:
    """Update an existing booking."""
    client = ctx.obj["client"]
    body = json.loads(data)
    result = client.patch(MODULE, f"bookings/{booking_id}", json_body=body)
    render_single(result, BOOKING_COLUMNS, as_json=ctx.obj["json"])


# --- Contacts ---


@app.command()
def contacts_list(
    ctx: typer.Context,
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List contacts."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    if all_pages:
        data = list(fetch_all(client, MODULE, "contacts", page_size=page_size))
        render(data, CONTACT_COLUMNS, as_json=as_json, title="Contacts")
    else:
        envelope = fetch_page(client, MODULE, "contacts", page=page, page_size=page_size)
        render(
            envelope.get("data", []),
            CONTACT_COLUMNS,
            as_json=as_json,
            title="Contacts",
            total_count=envelope.get("totalCount"),
        )


@app.command()
def contacts_get(
    ctx: typer.Context, contact_id: int = typer.Argument(..., help="Contact ID")
) -> None:
    """Get a single contact by ID."""
    client = ctx.obj["client"]
    data = client.get(MODULE, f"contacts/{contact_id}")
    render_single(data, CONTACT_COLUMNS, as_json=ctx.obj["json"])

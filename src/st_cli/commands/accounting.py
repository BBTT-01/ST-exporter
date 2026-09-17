"""Accounting commands: invoices, estimates, payments."""

from __future__ import annotations

from typing import Any, Optional

import typer

from st_cli.dates import apply_date_params
from st_cli.output import Column, render, render_single
from st_cli.pagination import fetch_all, fetch_page

MODULE = "accounting"

app = typer.Typer(help="Accounting — invoices, payments, GL, journal entries, AP")

INVOICE_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Number", "number"),
    ("Job ID", "jobId"),
    ("Customer ID", "customerId"),
    ("Status", "status"),
    ("Total", "total"),
    ("Created", "createdOn"),
]

PAYMENT_COLUMNS: list[Column] = [
    ("ID", "id"),
    ("Invoice ID", "invoiceId"),
    ("Type", "type"),
    ("Amount", "amount"),
    ("Date", "date"),
]


@app.command("invoices-list")
def invoices_list(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, help="Filter by invoice status"),
    range_val: Optional[str] = typer.Option(None, "--range", help="Date range (e.g. this-month)"),
    from_date: Optional[str] = typer.Option(
        None,
        "--from-date",
        help="Created on or after (YYYY-MM-DD)",
    ),
    to_date: Optional[str] = typer.Option(None, "--to-date", help="Created before (YYYY-MM-DD)"),
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List invoices."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    apply_date_params(params, range_val, from_date, to_date)

    if all_pages:
        data = list(fetch_all(client, MODULE, "invoices", params, page_size=page_size))
        render(data, INVOICE_COLUMNS, as_json=as_json, title="Invoices")
    else:
        envelope = fetch_page(client, MODULE, "invoices", params, page=page, page_size=page_size)
        render(
            envelope.get("data", []),
            INVOICE_COLUMNS,
            as_json=as_json,
            title="Invoices",
            total_count=envelope.get("totalCount"),
        )


@app.command("invoices-get")
def invoices_get(
    ctx: typer.Context, invoice_id: int = typer.Argument(..., help="Invoice ID")
) -> None:
    """Get a single invoice by ID."""
    client = ctx.obj["client"]
    data = client.get(MODULE, f"invoices/{invoice_id}")
    render_single(data, INVOICE_COLUMNS, as_json=ctx.obj["json"])


@app.command("payments-list")
def payments_list(
    ctx: typer.Context,
    range_val: Optional[str] = typer.Option(None, "--range", help="Date range (e.g. this-month)"),
    from_date: Optional[str] = typer.Option(
        None,
        "--from-date",
        help="Created on or after (YYYY-MM-DD)",
    ),
    to_date: Optional[str] = typer.Option(None, "--to-date", help="Created before (YYYY-MM-DD)"),
    page: int = typer.Option(1, help="Page number"),
    page_size: int = typer.Option(50, help="Page size"),
    all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
) -> None:
    """List payments."""
    client = ctx.obj["client"]
    as_json = ctx.obj["json"]
    params: dict[str, Any] = {}
    apply_date_params(params, range_val, from_date, to_date)

    if all_pages:
        data = list(fetch_all(client, MODULE, "payments", params, page_size=page_size))
        render(data, PAYMENT_COLUMNS, as_json=as_json, title="Payments")
    else:
        envelope = fetch_page(client, MODULE, "payments", params, page=page, page_size=page_size)
        render(
            envelope.get("data", []),
            PAYMENT_COLUMNS,
            as_json=as_json,
            title="Payments",
            total_count=envelope.get("totalCount"),
        )

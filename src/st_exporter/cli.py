"""``st-export`` — the exporter's CLI entrypoint (one command, run by GitHub Actions)."""

from __future__ import annotations

import typer

from st_cli.config import load_settings
from st_cli.exceptions import STCLIError
from st_exporter.config import ExporterSettings
from st_exporter.run import run_export


def run_once(
    pricebook: bool = typer.Option(
        False,
        help="No-op; reserved for a future price-book feed the CLI doesn't support yet.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compute the run but don't write to Google Sheets."
    ),
) -> None:
    """Run one ServiceTitan -> Export Store export pass."""
    st_settings = load_settings()
    exporter_settings = ExporterSettings()  # type: ignore[call-arg]
    summary = run_export(st_settings, exporter_settings, pricebook=pricebook, dry_run=dry_run)
    typer.echo(
        f"jobs={summary.jobs_row_count} technicians={summary.technicians_row_count} "
        f"skipped_no_job={summary.skipped_no_job} dry_run={summary.dry_run}"
    )


def main() -> None:
    """Entry point for the `st-export` command."""
    try:
        typer.run(run_once)
    except STCLIError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

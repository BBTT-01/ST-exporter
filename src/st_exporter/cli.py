"""``st-export`` — the exporter's CLI entrypoint (one command, run by GitHub Actions)."""

from __future__ import annotations

import typer
from pydantic import ValidationError

from st_cli.client import ServiceTitanClient
from st_cli.config import Settings, load_settings
from st_cli.exceptions import STCLIError
from st_exporter.config import ExporterSettings
from st_exporter.images.client import TrueQuoteImageClient
from st_exporter.logging_setup import logger
from st_exporter.outbox.client import TradeRatedOutboxClient
from st_exporter.outbox.drain import DrainSummary, drain_outbox
from st_exporter.outbox.ledger import OutboxLedger
from st_exporter.run import parse_feeds, run_export
from st_exporter.sheets import SheetsClient, get_gspread_client
from st_exporter.traderated_settings import TradeRatedSettings


def run_once(
    feeds: str = typer.Option(
        "jobs,technicians",
        "--feeds",
        help=(
            "Comma-separated feeds to run this call: jobs, technicians, pricebook, "
            "financial. `pricebook` writes the four pricebook.* tabs and `financial` "
            "the four Profit Wizard tabs (accounting.invoices, payroll.timesheets, "
            "settings.businessUnits, reporting.jobCosts); neither is on by default."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compute the run but don't write to Google Sheets."
    ),
    upload_images: bool = typer.Option(
        True,
        "--upload-images/--no-upload-images",
        help=(
            "With `--feeds pricebook`, also POST the pricebook image bytes to TrueQuote. "
            "Needs TRADERATED_IMAGE_TOKEN + TRADERATED_OUTBOX_BASE_URL; silently skipped "
            "without them. Identifiers still go in the Sheet either way."
        ),
    ),
) -> None:
    """Run one ServiceTitan -> Export Store export pass, then drain the CRM Outbox."""
    st_settings = load_settings()
    exporter_settings = ExporterSettings()  # type: ignore[call-arg]
    traderated_settings = TradeRatedSettings()

    parsed_feeds = parse_feeds(feeds)
    image_client = _image_client(traderated_settings, parsed_feeds, upload_images, dry_run)
    try:
        summary = run_export(
            st_settings,
            exporter_settings,
            feeds=parsed_feeds,
            dry_run=dry_run,
            image_client=image_client,
        )
    finally:
        if image_client is not None:
            image_client.close()

    outbox_summary: DrainSummary | None = None
    if not dry_run and traderated_settings.configured:
        try:
            outbox_summary = _drain_outbox(st_settings, exporter_settings, traderated_settings)
        except Exception as exc:
            # The export half already succeeded and committed its Sheets writes
            # by now, so an outbox problem must not fail the whole run — and an
            # httpx error escaping drain_outbox (e.g. claim() itself failing) is
            # neither STCLIError nor ValidationError, so main()'s handler would
            # not catch it and the run would end in a raw traceback. Leaving
            # outbox_summary as None just omits the outbox fields from the echoed
            # line, exactly like the not-configured and dry-run paths.
            # `Exception`, not bare `except`: SystemExit/KeyboardInterrupt must
            # still propagate.
            logger.warning("outbox drain failed; export results are unaffected: %s", exc)
    elif not traderated_settings.configured:
        # Expected until ticket 07 issues the machine token/outbox URL — not an
        # error, so this is INFO, not WARNING.
        logger.info("TRADERATED_MACHINE_TOKEN/OUTBOX_BASE_URL not set; skipping outbox drain")

    message = (
        f"jobs={summary.jobs_row_count} technicians={summary.technicians_row_count} "
        f"skipped_no_job={summary.skipped_no_job} dry_run={summary.dry_run}"
    )
    if summary.pricebook_row_counts is not None:
        message += "".join(
            f" {tab.replace('.', '_')}={count}"
            for tab, count in sorted(summary.pricebook_row_counts.items())
        )
    if summary.financial_row_counts is not None:
        message += "".join(
            f" {_tab_key(tab)}={count}"
            for tab, count in sorted(summary.financial_row_counts.items())
        )
    if summary.financial_failures:
        # Named in the run's output rather than only in the log: a missing
        # `reporting.jobCosts` tab is the difference between Profit Wizard costing
        # jobs and not, and it must not be something you have to go digging for.
        message += " financial_failed=" + ",".join(sorted(summary.financial_failures))
    if summary.images is not None:
        images = summary.images
        message += (
            f" images_uploaded={images.uploaded} images_already={images.already_uploaded} "
            f"images_failed={images.download_failed + images.upload_rejected} "
            f"images_permission_denied={str(images.permission_denied).lower()}"
        )
    if outbox_summary is not None:
        message += (
            f" outbox_claimed={outbox_summary.claimed} outbox_succeeded={outbox_summary.succeeded} "
            f"outbox_failed={outbox_summary.failed} outbox_replayed={outbox_summary.replayed}"
        )
    typer.echo(message)


def _tab_key(tab_name: str) -> str:
    """`accounting.invoices` -> `accounting_invoices`, for the flat summary line."""
    return tab_name.replace(".", "_")


def _image_client(
    traderated_settings: TradeRatedSettings,
    feeds: frozenset[str],
    upload_images: bool,
    dry_run: bool,
) -> TrueQuoteImageClient | None:
    """The image-upload client, or None when this run has no business uploading.

    Gated the same way the outbox drain is: a missing token is the EXPECTED
    state until TrueQuote issues one, so it is INFO and a skip, never an error.
    The token is the `image_upload`-scoped one — presenting the booking token
    here earns a 401 from TrueQuote, not a fallback.
    """
    if not upload_images or dry_run or "pricebook" not in feeds:
        return None
    if not traderated_settings.images_configured:
        logger.info("TRADERATED_IMAGE_TOKEN/OUTBOX_BASE_URL not set; skipping image upload")
        return None
    assert traderated_settings.image_token
    assert traderated_settings.outbox_base_url
    return TrueQuoteImageClient(
        traderated_settings.outbox_base_url, traderated_settings.image_token
    )


def _drain_outbox(
    st_settings: Settings,
    exporter_settings: ExporterSettings,
    traderated_settings: TradeRatedSettings,
) -> DrainSummary:
    # Truthiness, not `is not None`, to match `configured` exactly — an empty
    # string is a value pydantic will happily load and httpx will not accept.
    assert traderated_settings.machine_token
    assert traderated_settings.outbox_base_url

    gc = get_gspread_client(exporter_settings.service_account_json)
    raw_cache_store = SheetsClient.open(gc, exporter_settings.raw_cache_sheet_id)
    ledger = OutboxLedger(raw_cache_store)

    client = ServiceTitanClient(st_settings)
    outbox_client = TradeRatedOutboxClient(
        traderated_settings.outbox_base_url, traderated_settings.machine_token
    )
    try:
        return drain_outbox(client, outbox_client, ledger)
    finally:
        client.close()
        outbox_client.close()


def main() -> None:
    """Entry point for the `st-export` command."""
    try:
        typer.run(run_once)
    except (STCLIError, ValidationError) as exc:
        # ValidationError covers a missing/malformed env var surfacing from
        # load_settings()/ExporterSettings() — those aren't STCLIError subclasses,
        # so without this a bad Actions secret prints a raw pydantic traceback
        # instead of the same clean Error: ... + exit-1 path. parse_feeds() raising
        # ConfigError (an STCLIError) on a bad --feeds value takes the same path.
        #
        # Deliberately SystemExit, not typer.Exit: this except block runs after
        # typer.run(run_once) has already returned control to us, outside any
        # Click/Typer dispatch loop, so nothing would translate a typer.Exit into
        # an actual clean process exit here — it would just be an uncaught
        # exception (Python prints a traceback and exits 1 anyway, but with a
        # traceback dumped on top of the "Error: ..." line, defeating the point).
        # SystemExit is what Python's interpreter itself treats specially: a
        # clean exit with no traceback.
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

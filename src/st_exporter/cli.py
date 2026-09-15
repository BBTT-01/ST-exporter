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
from st_exporter.outbox.drain import LaneOutcome, drain_lanes
from st_exporter.outbox.lanes import build_lanes, close_lanes
from st_exporter.outbox.ledger import OutboxLedger
from st_exporter.outbox.settings import load_all_lane_credentials
from st_exporter.run import EXPORT_FEEDS, OUTBOX_FEED, ExportSummary, parse_feeds, run_export
from st_exporter.sheets import SheetsClient, get_gspread_client
from st_exporter.traderated_settings import TradeRatedSettings


def run_once(
    feeds: str = typer.Option(
        "jobs,technicians",
        "--feeds",
        help=(
            "Comma-separated feeds to run this call: jobs, technicians, pricebook, "
            "financial, outbox. `pricebook` writes the four pricebook.* tabs and "
            "`financial` the four Profit Wizard tabs (accounting.invoices, "
            "payroll.timesheets, settings.businessUnits, reporting.jobCosts); neither "
            "is on by default. `outbox` writes no tab at all — it drains the outbox "
            "queue of every product whose secrets are set, and it must appear in "
            "EXACTLY ONE workflow job."
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
            "Needs TRUEQUOTE_IMAGE_TOKEN + TRUEQUOTE_OUTBOX_URL (the TRADERATED_* "
            "spellings are still accepted); silently skipped "
            "without them. Identifiers still go in the Sheet either way."
        ),
    ),
) -> None:
    """Run one ServiceTitan -> Export Store export pass, then drain the outboxes."""
    st_settings = load_settings()
    exporter_settings = ExporterSettings()  # type: ignore[call-arg]
    traderated_settings = TradeRatedSettings()

    parsed_feeds = parse_feeds(feeds)
    export_feeds = parsed_feeds & EXPORT_FEEDS
    drain_requested = OUTBOX_FEED in parsed_feeds

    summary: ExportSummary | None = None
    if export_feeds:
        image_client = _image_client(traderated_settings, export_feeds, upload_images, dry_run)
        try:
            summary = run_export(
                st_settings,
                exporter_settings,
                feeds=export_feeds,
                dry_run=dry_run,
                image_client=image_client,
            )
        finally:
            if image_client is not None:
                image_client.close()

    outcomes: list[LaneOutcome] = []
    if drain_requested and not dry_run:
        try:
            outcomes = _drain_outboxes(st_settings, exporter_settings)
        except Exception as exc:
            # Any export half has already succeeded and committed its Sheets
            # writes by now, so an outbox problem must not fail the whole run —
            # and an httpx error escaping the drain is neither STCLIError nor
            # ValidationError, so main()'s handler would not catch it and the run
            # would end in a raw traceback. `Exception`, not bare `except`:
            # SystemExit/KeyboardInterrupt must still propagate.
            #
            # `drain_lanes` already isolates each lane from the others, so
            # reaching here means something outside any single lane broke —
            # opening the ledger Sheet, say.
            logger.warning("outbox drain failed; export results are unaffected: %s", exc)
    elif not drain_requested:
        _warn_if_undrained()

    typer.echo(_summary_line(summary, outcomes))


def _warn_if_undrained() -> None:
    """Say loudly when a purchased product's queue was not drained by this call.

    Silence is what makes the double-drain trap dangerous in reverse: a
    connector repo that bumps to this version without adding `outbox` to one of
    its jobs would simply stop draining, and nobody would learn about it from a
    green run. So every invocation that carries a product's secrets but did not
    ask for the drain names that product at WARNING.

    After the caller workflow is migrated this is silent, because the jobs that
    do not drain are not given the secrets either.
    """
    configured = [credentials.product for credentials in load_all_lane_credentials()]
    if not configured:
        return
    logger.warning(
        "outbox secrets are set for %s but this run did not request the `outbox` feed, "
        "so NOTHING was drained. Exactly one job in the caller workflow must pass "
        "`feeds: outbox`.",
        ", ".join(configured),
    )


def _summary_line(summary: ExportSummary | None, outcomes: list[LaneOutcome]) -> str:
    """The one line this run echoes. Drain-only runs have no export half."""
    message = ""
    if summary is not None:
        message = (
            f"jobs={summary.jobs_row_count} technicians={summary.technicians_row_count} "
            f"skipped_no_job={summary.skipped_no_job} dry_run={summary.dry_run}"
        )
        if summary.pricebook_row_counts is not None:
            message += "".join(
                f" {tab.replace('.', '_')}={count}"
                for tab, count in sorted(summary.pricebook_row_counts.items())
            )
        if summary.pricebook_failures:
            # Named in the run's output, not only in the log — a pricebook tab
            # left at last run's contents is a fact whoever reads the catalogue
            # needs, and it must not be something you have to go digging for.
            message += " pricebook_failed=" + ",".join(
                _tab_key(tab) for tab in sorted(summary.pricebook_failures)
            )
        if summary.financial_row_counts is not None:
            message += "".join(
                f" {_tab_key(tab)}={count}"
                for tab, count in sorted(summary.financial_row_counts.items())
            )
        if summary.financial_failures:
            # Named in the run's output rather than only in the log: a missing
            # `reporting.jobCosts` tab is the difference between Profit Wizard
            # costing jobs and not, and it must not be something you have to go
            # digging for.
            message += " financial_failed=" + ",".join(sorted(summary.financial_failures))
        if summary.images is not None:
            images = summary.images
            message += (
                f" images_uploaded={images.uploaded} images_already={images.already_uploaded} "
                f"images_failed={images.download_failed + images.upload_rejected} "
                f"images_permission_denied={str(images.permission_denied).lower()} "
                # `stopped` is the one that changes what the counts MEAN: a pass
                # that aborted has "not looked at" the rest of the catalogue, so
                # images_uploaded=0 reads as "nothing to do" unless this says
                # otherwise. Named in the run's output, not only in the log —
                # the same rule as pricebook_failed / financial_failed above.
                f"images_stopped={images.stopped or 'no'} "
                f"images_too_large={images.too_large} "
                f"images_unsupported={images.unsupported}"
            )

    for outcome in outcomes:
        # Prefixed per product, so a two-lane run reads as two sets of counters
        # rather than as one set nobody can attribute.
        if outcome.summary is not None:
            counts = outcome.summary
            message += (
                f" {outcome.product}_claimed={counts.claimed} "
                f"{outcome.product}_succeeded={counts.succeeded} "
                f"{outcome.product}_failed={counts.failed} "
                f"{outcome.product}_replayed={counts.replayed}"
            )
        else:
            message += f" {outcome.product}_lane_error=1"

    return message.strip() or "nothing to do"


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
        logger.info("TRUEQUOTE_IMAGE_TOKEN/TRUEQUOTE_OUTBOX_URL not set; skipping image upload")
        return None
    token = traderated_settings.image_upload_token
    base_url = traderated_settings.image_base_url
    assert token
    assert base_url
    return TrueQuoteImageClient(base_url, token)


def _drain_outboxes(
    st_settings: Settings,
    exporter_settings: ExporterSettings,
) -> list[LaneOutcome]:
    """Build every configured lane and drain them, sharing one ledger and client.

    One ``OutboxLedger`` across all lanes on purpose: it is a single tab read and
    rewritten whole, so a ledger per lane would have each lane's flush erase the
    others' rows. Keys are namespaced by product inside it, not by tab.
    """
    credentials = load_all_lane_credentials()
    if not credentials:
        # Expected for a contractor who bought a product whose write lane they do
        # not use — not an error, so INFO.
        logger.info("no outbox secrets are set; nothing to drain")
        return []

    client = ServiceTitanClient(st_settings)
    lanes, skipped = build_lanes(credentials, client)
    for lane in skipped:
        logger.warning("outbox lane %s not drained: %s", lane.product, lane.reason)

    if not lanes:
        client.close()
        return []

    gc = get_gspread_client(exporter_settings.service_account_json)
    raw_cache_store = SheetsClient.open(gc, exporter_settings.raw_cache_sheet_id)
    ledger = OutboxLedger(raw_cache_store)

    try:
        return drain_lanes(client, lanes, ledger)
    finally:
        client.close()
        close_lanes(lanes)


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

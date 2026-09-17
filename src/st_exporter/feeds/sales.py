"""Fetch for the `sales.estimates` tab: ServiceTitan's Sales & Estimates API.

``CLAUDE.md`` records that estimates live in the ``salestech`` group under the
``sales`` API module — a prior routing bug had them under ``accounting`` — so the
module name below is checked against ``st_cli/registry.py`` (``Module(name="sales",
group="salestech", ...)``) rather than assumed.

Window-bounded like the other three `financial` tabs that grow without bound
(invoices, timesheets, job costs): estimates accumulate forever, and re-listing a
tenant's whole estimate history every six hours would be pure waste. Shares
``financial.window_start``/``FINANCIAL_WINDOW_DAYS`` so all `financial`-feed tabs
agree on what "the window" means for one run.

The date-filter parameter name is an unverified guess (see ``KNOWN_UNVERIFIED.md``,
"sales.estimates field spellings") — there was no real tenant to confirm it
against. ``warn_if_older_than_window`` is reused from ``feeds/financial.py`` for
the same reason it exists there: ServiceTitan ignores an unrecognised query
parameter rather than rejecting it, so a wrong spelling here would quietly export
the tenant's entire estimate history instead of the window, and this is the one
tripwire that catches it on a real run.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from st_cli.client import ServiceTitanClient
from st_cli.pagination import fetch_all
from st_exporter.feeds.financial import warn_if_older_than_window, window_start
from st_exporter.window import FINANCIAL_WINDOW_DAYS

MODULE = "sales"
RESOURCE = "estimates"

#: Unverified — see the module docstring and KNOWN_UNVERIFIED.md.
ESTIMATE_DATE_PARAM = "modifiedOnOrAfter"

_PAGE_SIZE = 200


def fetch_estimates(
    client: ServiceTitanClient,
    *,
    today: date,
    window_days: int = FINANCIAL_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Estimates modified on or after the financial window's start, items included."""
    start = window_start(today, window_days=window_days)
    records = list(
        fetch_all(
            client,
            MODULE,
            RESOURCE,
            params={ESTIMATE_DATE_PARAM: _iso(start)},
            page_size=_PAGE_SIZE,
        )
    )
    warn_if_older_than_window(records, field="modifiedOn", start=start, param=ESTIMATE_DATE_PARAM)
    return records


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")

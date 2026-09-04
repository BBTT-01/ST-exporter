"""Small, slow-changing reference lookups: full refresh every run, no cursor.

Deliberately different treatment from the five high-volume feeds in this package:
technicians, job types and business units are small (dozens to low hundreds of
rows), so a full re-fetch every run is simpler and self-healing — no cursor bugs
possible — and avoids needing a `_raw_technicians`/etc. cache in the private
raw-cache Sheet for data this cheap to just re-read.
"""

from __future__ import annotations

from typing import Any

from st_cli.client import ServiceTitanClient
from st_cli.pagination import fetch_all

_PAGE_SIZE = 200


def fetch_technicians(client: ServiceTitanClient) -> list[dict[str, Any]]:
    """Full list from ``settings/v2/tenant/{id}/technicians``.

    Passes ``active=Any`` — ServiceTitan settings list endpoints conventionally
    default to active-only, which would make a deactivated technician silently
    disappear from the tab instead of showing up with ``active=false``. The exact
    parameter name/values aren't confirmed against a real tenant — see
    KNOWN_UNVERIFIED.md.
    """
    return list(
        fetch_all(client, "settings", "technicians", params={"active": "Any"}, page_size=_PAGE_SIZE)
    )


def fetch_job_types(client: ServiceTitanClient) -> dict[str, dict[str, Any]]:
    """Full list from ``jpm/v2/tenant/{id}/job-types``, keyed by id (as str)."""
    records = fetch_all(client, "jpm", "job-types", page_size=_PAGE_SIZE)
    return {str(record["id"]): record for record in records if "id" in record}


def fetch_business_units(client: ServiceTitanClient) -> dict[str, dict[str, Any]]:
    """Full list from ``settings/v2/tenant/{id}/business-units``, keyed by id (as str)."""
    records = fetch_all(client, "settings", "business-units", page_size=_PAGE_SIZE)
    return {str(record["id"]): record for record in records if "id" in record}

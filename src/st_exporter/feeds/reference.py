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
from st_cli.exceptions import APIError
from st_cli.pagination import fetch_all
from st_exporter.logging_setup import announce_to_actions, logger

_PAGE_SIZE = 200

#: Requests deactivated technicians alongside active ones. Still unverified against
#: a real tenant (KNOWN_UNVERIFIED.md), which is exactly why it is not removed on a
#: guess either: dropping it would silently make the tab active-only, and a
#: deactivated technician vanishing from the tab is indistinguishable, downstream,
#: from one who never existed. The parameter stays; the FAILURE mode is what got
#: fixed — see ``fetch_technicians``.
_ACTIVE_ANY = {"active": "Any"}


def fetch_technicians(client: ServiceTitanClient) -> list[dict[str, Any]]:
    """Full list from ``settings/v2/tenant/{id}/technicians``.

    Passes ``active=Any`` — ServiceTitan settings list endpoints conventionally
    default to active-only, which would make a deactivated technician silently
    disappear from the tab instead of showing up with ``active=false``. The exact
    parameter name/values aren't confirmed against a real tenant — see
    KNOWN_UNVERIFIED.md.

    **On a 400, and only a 400, the list is re-fetched without the parameter.**
    A 400 is ServiceTitan saying it does not accept this filter, which is the one
    reading under which sending it is pointless; every other status (403 on a
    missing Settings → Technicians permission, 429, 5xx, a transport failure) is
    about the request's fate, not the parameter, and is re-raised for the caller's
    guard to handle. This is not a guess about the right spelling — it cannot be
    made without a tenant — it makes the wrong guess SURVIVABLE: a rejected filter
    now costs an active-only tab plus a loud annotation naming what is missing,
    instead of failing the whole feed. Remove the fallback once a tenant confirms
    the parameter.
    """
    try:
        return list(
            fetch_all(
                client, "settings", "technicians", params=dict(_ACTIVE_ANY), page_size=_PAGE_SIZE
            )
        )
    except APIError as exc:
        if exc.status_code != 400:
            raise
        logger.warning(
            "DEGRADED: ServiceTitan rejected active=Any on the technicians list (%s). "
            "Re-fetching WITHOUT it — the technicians tab may therefore be "
            "active-only, so a deactivated technician can be missing from it "
            "entirely rather than present with active=false. See "
            "KNOWN_UNVERIFIED.md, 'Technician `active` filter parameter'.",
            exc,
        )
        announce_to_actions(
            "Technicians: active=Any rejected",
            f"ServiceTitan returned {exc} for the technicians list with active=Any. "
            f"The tab was rebuilt WITHOUT that filter and may be active-only: a "
            f"deactivated technician can be absent rather than active=false. The "
            f"correct parameter needs confirming on a real tenant "
            f"(KNOWN_UNVERIFIED.md).",
        )
        return list(fetch_all(client, "settings", "technicians", page_size=_PAGE_SIZE))


def fetch_job_types(client: ServiceTitanClient) -> dict[str, dict[str, Any]]:
    """Full list from ``jpm/v2/tenant/{id}/job-types``, keyed by id (as str)."""
    records = fetch_all(client, "jpm", "job-types", page_size=_PAGE_SIZE)
    return {str(record["id"]): record for record in records if "id" in record}


def fetch_business_units(client: ServiceTitanClient) -> dict[str, dict[str, Any]]:
    """Full list from ``settings/v2/tenant/{id}/business-units``, keyed by id (as str)."""
    records = fetch_all(client, "settings", "business-units", page_size=_PAGE_SIZE)
    return {str(record["id"]): record for record in records if "id" in record}

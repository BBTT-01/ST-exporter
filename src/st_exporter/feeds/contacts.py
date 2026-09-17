"""Customer contact details — the endpoint a phone number and an email really live on.

``customer_phone`` and ``customer_email`` were blank on **every** `jobs` row ever
exported, on both live tenants (2,441 rows on one, 1,068 on the other). The
exporter read them off the customer record — ``customer.phone``,
``phoneSettings[]``, ``contacts[]`` — and ServiceTitan's export feed puts them in
none of those places. They live on a separate sub-resource:

    GET /crm/v2/tenant/{tenantId}/customers/{customerId}/contacts
    -> {"data": [{"type": "Email" | "MobilePhone" | "Phone" | "Fax", "value": ...}]}

This is not another guess. TradeRated's own live Direct-path function has been
reading exactly this endpoint in production for the same two contractors
(``traderatedapp/supabase/functions/get-servicetitan-technician-jobs/index.ts``,
"Fetch customer contacts (email/phone are stored separately)"). The selection
rule here is deliberately the same one, so a contractor cannot see a different
phone number depending on whether Hosted or Direct served the row — see
``select_contact_value``.

**Which route.** The Direct function fetches contacts one job at a time. For an
exporter that is N+1 against a 600-request/10s budget, so a bulk route was looked
for first and NOT found:

* ``registry.py`` declares the crm module's export change-feeds as
  ``("customers", "locations", "bookings")`` — no contacts feed;
* no public documentation of a ``crm/v2/tenant/{id}/export/customers/contacts``
  feed could be found, and third-party ServiceTitan connectors enumerate customer
  contacts only as the per-customer CRUD sub-resource;
* it cannot be settled either way without a live tenant to ask.

So the route is a CHOICE, not a guess: ``EXPORTER_CONTACTS_ROUTE`` selects
:data:`ROUTE_PER_CUSTOMER` (the default — the one route with live evidence behind
it) or :data:`ROUTE_EXPORT` (the bulk change-feed, if a tenant turns out to have
it). Turning the bulk route on is a safe experiment: a 404/400 from it is
ServiceTitan saying the feed does not exist, which is announced and falls back to
the per-customer route for that run rather than blanking the two columns.

Per-customer volume, deduped by ``customerId``: a tenant with ~2,400 job rows is
well under 2,400 distinct customers (rows are one per assigned technician per
appointment, and one customer commonly has several jobs) — call it 1,000–1,500
requests per run, capped by ``EXPORTER_CONTACTS_MAX_CUSTOMERS``. That is inside
the rate limit but it is not free, which is why the cap exists and why hitting it
is loud rather than a quietly short tab.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from st_cli.client import ServiceTitanClient
from st_cli.exceptions import APIError
from st_cli.pagination import fetch_all, fetch_export_all
from st_exporter.logging_setup import announce_to_actions, logger

MODULE = "crm"

#: The bulk change-feed this module will use if a tenant proves to have it:
#: ``crm/v2/tenant/{id}/export/customers/contacts``. Unverified — see the module
#: docstring; reached only via ``EXPORTER_CONTACTS_ROUTE=export``.
EXPORT_FEED = "customers/contacts"

#: Cursor key for that feed inside the jobs `_meta` cursor bundle.
CURSOR_KEY = "customer-contacts"

ROUTE_PER_CUSTOMER = "per-customer"
ROUTE_EXPORT = "export"
ROUTES: tuple[str, ...] = (ROUTE_PER_CUSTOMER, ROUTE_EXPORT)

#: Cap on how many customers one run asks for contacts. Same shape and same
#: reason as the financial feed's ``EXPORTER_FINANCIAL_MAX_JOBS``: an N+1 pull
#: against a throttled API must be bounded, and the bound must be visible when it
#: bites. Sized for both live tenants' whole customer sets with room to spare.
DEFAULT_MAX_CONTACT_CUSTOMERS = 3000

_PAGE_SIZE = 200

#: Phone contact types, **in preference order**: a mobile is the number a
#: technician actually wants. ``Fax`` is deliberately absent and is never a
#: fallback — a fax number on a technician's screen is a wrong answer, which is
#: worse than a blank one. Matched case-folded.
PHONE_TYPES_IN_PREFERENCE_ORDER: tuple[str, ...] = ("mobilephone", "phone")
EMAIL_TYPES_IN_PREFERENCE_ORDER: tuple[str, ...] = ("email",)


def select_contact_value(
    contacts: Iterable[Any],
    types_in_preference_order: tuple[str, ...],
) -> Any:
    """The best contact value of the given types, or ``None``.

    Selection is by ``type`` and never by position — index 0 of a customer's
    contacts can just as easily be their fax number, and a fax in the phone
    column is a wrong answer. Types are tried in the order given (so
    ``MobilePhone`` beats ``Phone`` however the array is ordered) and, within one
    type, the first non-blank value wins.

    **Parity with the Direct path.** TradeRated's live Direct function does
    ``contacts.find(c => c.type === 'MobilePhone' || c.type === 'Phone')``, whose
    comment says "prefer MobilePhone, fallback to any phone type". The comment
    describes this function; the code describes array order. The two agree except
    for a customer carrying BOTH types with the landline listed first, where
    Direct returns the landline and this returns the mobile. The stated intent is
    followed here (and the `jobs` tab has one cell, so it has to pick); the
    Direct function is the side that should be brought into line.
    """
    for wanted in types_in_preference_order:
        for contact in contacts or []:
            if not isinstance(contact, Mapping):
                continue
            if str(contact.get("type") or "").strip().lower() != wanted:
                continue
            value = contact.get("value")
            if value is not None and str(value).strip():
                return value
    return None


def group_contacts_by_customer(records: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """Index flat contact records by ``customerId`` (as str), order preserved."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        customer_id = record.get("customerId")
        if customer_id is None:
            continue
        grouped.setdefault(str(customer_id), []).append(dict(record))
    return grouped


def fetch_contacts_per_customer(
    client: ServiceTitanClient,
    customer_ids: Iterable[Any],
    *,
    max_customers: int = DEFAULT_MAX_CONTACT_CUSTOMERS,
) -> dict[str, list[dict[str, Any]]]:
    """``customers/{id}/contacts`` for each DISTINCT customer id, capped.

    Ids are deduped in the order given (the caller passes them in the `jobs`
    tab's own deterministic row order), because many jobs share one customer and
    an un-deduped pull would be one request per ROW rather than per customer.

    A 404 OR a 409 on one customer is skipped rather than raised. A 404 means a
    customer can be merged or deleted between the export feed and this call; a
    409 is ServiceTitan's answer for an INACTIVE customer ("Customer ID = <id>
    is not active"), observed live against the Door Serv Pro tenant. Either
    way, losing both contact columns for the WHOLE tenant over one customer
    would be a self-inflicted outage that repeats every run — exactly what
    happened before this fix, when a single inactive customer's 409 escaped
    per-customer handling, propagated out of this loop, and made the caller's
    degradation guard blank `customer_phone`/`customer_email` on every row.
    Every other error (a 403 on a missing CRM permission above all) is
    re-raised for the caller's degradation guard, so THAT failure stays
    visible instead of presenting as "this contractor has no phone numbers".
    """
    wanted = list(dict.fromkeys(str(cid) for cid in customer_ids if cid is not None and cid != ""))
    if len(wanted) > max_customers:
        logger.warning(
            "contacts: %d customers need contact details but the cap is %d — the "
            "remaining %d keep blank customer_phone/customer_email cells this run. "
            "Raise EXPORTER_CONTACTS_MAX_CUSTOMERS if this tenant needs more.",
            len(wanted),
            max_customers,
            len(wanted) - max_customers,
        )
        announce_to_actions(
            "Customer contacts capped",
            f"{len(wanted)} customers needed contact details, cap is {max_customers}. "
            f"{len(wanted) - max_customers} job row(s) keep a blank customer_phone / "
            f"customer_email this run. Raise EXPORTER_CONTACTS_MAX_CUSTOMERS.",
        )
        wanted = wanted[:max_customers]

    contacts: dict[str, list[dict[str, Any]]] = {}
    missing = 0
    inactive = 0
    for customer_id in wanted:
        try:
            records = list(
                fetch_all(client, MODULE, f"customers/{customer_id}/contacts", page_size=_PAGE_SIZE)
            )
        except APIError as exc:
            if exc.status_code == 404:
                missing += 1
                continue
            if exc.status_code == 409:
                inactive += 1
                continue
            raise
        contacts[customer_id] = [dict(record) for record in records if isinstance(record, Mapping)]
    if missing:
        logger.info("contacts: %d customer(s) answered 404 and keep blank contact cells", missing)
    if inactive:
        logger.info(
            "contacts: %d customer(s) answered 409 (inactive customer) and keep blank "
            "contact cells",
            inactive,
        )
    logger.info("contacts: fetched contacts for %d customer(s) (per-customer route)", len(contacts))
    return contacts


def fetch_contacts_export_delta(
    client: ServiceTitanClient, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Drain the bulk ``crm/export/customers/contacts`` change-feed from ``cursor``.

    Same incremental contract as the five feeds in ``_export.py``; kept here
    rather than beside them because, unlike those, this feed is not known to
    exist — see the module docstring. ``APIError`` is left to the caller, which
    treats a 404/400 as "this tenant has no such feed" and falls back.
    """
    return fetch_export_all(client, MODULE, EXPORT_FEED, continue_from=cursor)

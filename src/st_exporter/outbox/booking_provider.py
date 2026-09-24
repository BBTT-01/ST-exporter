"""Find-or-create the one Booking Provider Tag every TrueQuote booking is filed under.

ServiceTitan files a booking under a *booking provider*: ``POST crm/booking-
provider/{id}/bookings``. Before this module, the id came from TrueQuote's
database on every queued item (``booking_provider_id``), which meant somebody
had to create a Booking Provider Tag by hand inside the contractor's
ServiceTitan and type its id into TrueQuote. For a Hosted company the runner
already holds the tenant's credentials, so it owns the tag instead: look for one
named ``TrueQuote``, create it on the first booking that needs it, and reuse it
forever after. TrueQuote stops sending the id at all
(TrueQuote ``servicetitan-hosted-parity`` spec, "Booking without a provider id").

Same shape as :class:`~st_exporter.outbox.campaign.ReferralCampaign`, for the
same reasons: resolved lazily (a run with no booking makes no tag call), cached
for the lane's whole drain (ten bookings cost one lookup, not ten), and matched
by name over the full list rather than trusting a server-side filter.

**Never a duplicate.** A tag is created only after the full, paginated list has
been read and holds no ``TrueQuote`` tag at all — active or not. An inactive
match is refused with instructions rather than shadowed by a second tag: two
tags with one name would split the contractor's booking attribution in half,
and there is no API call that merges them afterwards.

**A 403 names the permission.** ServiceTitan's own text ("Scope validation
failed … ``GET /tenant/{tenant}/booking-provider-tags``") does not say which box
to tick. The error raised here does, and it is cached so every remaining item
in the batch fails with the same sentence instead of making its own 403 call.

That the booking provider id IS the Booking Provider Tag's id is still
unconfirmed live (``KNOWN_UNVERIFIED.md``). So the first resolution logs the
whole tag list — ids and names — which is the evidence that settles it.
"""

from __future__ import annotations

from typing import Any

from st_cli.client import ServiceTitanClient
from st_cli.pagination import fetch_all
from st_exporter.logging_setup import announce_to_actions, logger
from st_exporter.scopes import is_permission_denied

BOOKING_PROVIDER_TAG_NAME = "TrueQuote"

#: The ServiceTitan tick-boxes this module needs, in the words of the app
#: registration screen. Read to find the tag, Write to create it once.
BOOKING_PROVIDER_TAGS_PERMISSION = "CRM -> Booking Provider Tags (Read + Write)"

_TAG_DESCRIPTION = "Quote requests booked from the TrueQuote website widget"
_PAGE_SIZE = 200
_MODULE = "crm"
_RESOURCE = "booking-provider-tags"


class BookingProviderTagError(Exception):
    """The TrueQuote booking provider tag could neither be found nor created."""


class TrueQuoteBookingProvider:
    """Lazily resolves ``BOOKING_PROVIDER_TAG_NAME`` to a Booking Provider Tag id."""

    def __init__(self, client: ServiceTitanClient, name: str = BOOKING_PROVIDER_TAG_NAME) -> None:
        self._client = client
        self._name = name
        self._tag_id: int | None = None
        self._error: BookingProviderTagError | None = None

    def tag_id(self) -> int:
        """The tag id, resolving (and creating if needed) on first call.

        A failure is cached as well as a success: the answer cannot change
        mid-run, and a permission the app lacks on item one is still lacking on
        item ten.
        """
        if self._error is not None:
            raise self._error
        if self._tag_id is None:
            try:
                found = self._find()
                self._tag_id = found if found is not None else self._create()
            except BookingProviderTagError as exc:
                self._error = exc
                raise
        return self._tag_id

    def _find(self) -> int | None:
        wanted = self._name.casefold()
        try:
            tags = list(fetch_all(self._client, _MODULE, _RESOURCE, page_size=_PAGE_SIZE))
        except Exception as exc:
            raise self._describe(exc, "list") from exc

        logger.info(
            "booking provider tags in this tenant: %s",
            ", ".join(f"{tag.get('id')}={_tag_name(tag)!r}" for tag in tags) or "(none)",
        )

        matches = [tag for tag in tags if _tag_name(tag).casefold() == wanted and "id" in tag]
        active = sorted(int(tag["id"]) for tag in matches if tag.get("active", True))
        if active:
            if len(active) > 1:
                logger.warning(
                    "%d active booking provider tags are named %r (%s); using the lowest id",
                    len(active),
                    self._name,
                    ", ".join(str(tag_id) for tag_id in active),
                )
            logger.info("booking provider tag %r resolved to id %s", self._name, active[0])
            return active[0]

        if matches:
            inactive = ", ".join(str(tag["id"]) for tag in matches)
            raise BookingProviderTagError(
                f"the {self._name!r} booking provider tag exists but is inactive (id {inactive}). "
                "Reactivate it in ServiceTitan under Settings -> Integrations -> Booking "
                "Providers; a second tag is not created, because two tags with one name "
                "would split the contractor's booking attribution"
            )
        return None

    def _create(self) -> int:
        body = {"tagName": self._name, "description": _TAG_DESCRIPTION}
        logger.info("creating booking provider tag %r", self._name)
        try:
            created = self._client.post(_MODULE, _RESOURCE, json_body=body)
        except Exception as exc:
            raise self._describe(exc, "create") from exc

        tag_id = created.get("id") if isinstance(created, dict) else None
        if tag_id is None:
            raise BookingProviderTagError(
                f"ServiceTitan accepted the {self._name!r} booking provider tag but "
                f"returned no id: {created!r}"
            )
        logger.info("booking provider tag %r created with id %s", self._name, tag_id)
        return int(tag_id)

    def _describe(self, exc: Exception, verb: str) -> BookingProviderTagError:
        if not is_permission_denied(exc):
            return BookingProviderTagError(
                f"could not {verb} the {self._name!r} booking provider tag, so no TrueQuote "
                f"booking can be filed: {exc}"
            )
        message = (
            f"ServiceTitan refused to {verb} booking provider tags (HTTP 403), so no TrueQuote "
            f"booking can be filed. The contractor's ServiceTitan app is missing the "
            f"{BOOKING_PROVIDER_TAGS_PERMISSION} permission: tick it in the ServiceTitan "
            "Developer Portal and re-authorise the app"
        )
        announce_to_actions("TrueQuote booking provider tag", message, level="error")
        return BookingProviderTagError(message)


def _tag_name(tag: dict[str, Any]) -> str:
    name = tag.get("tagName", tag.get("name"))
    return name if isinstance(name, str) else ""

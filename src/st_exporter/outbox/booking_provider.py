"""Find-or-create the "TrueQuote" Booking Provider Tag a Hosted runner files bookings under.

A tag is created only when the full list holds none by that name, so it is never duplicated.
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
        wanted = self._name.strip().casefold()
        try:
            # No `active` filter: the CRM v2 schema's BookingProviderTags_GetList has none
            # (unlike pricebook's ActiveRequestArg), and an unknown one risks a 400.
            tags = list(fetch_all(self._client, _MODULE, _RESOURCE, page_size=_PAGE_SIZE))
        except Exception as exc:
            raise self._describe(exc, "list") from exc

        logger.info(
            "booking provider tags in this tenant: %s",
            ", ".join(f"{tag.get('id')}={_tag_name(tag)!r}" for tag in tags) or "(none)",
        )

        matches = [
            tag for tag in tags if _tag_name(tag).strip().casefold() == wanted and "id" in tag
        ]
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

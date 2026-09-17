"""TradeRated Outbox settings, loaded from environment only.

Mirrors ``st_exporter.config.ExporterSettings``'s no-``.env``-fallback rule — a
GitHub Actions runner has none, so there must be no code path that reads one.

Optional by design: ticket 06 must be buildable and mergeable before ticket 07
issues the machine token and outbox URL (the handoff brief: "Neither blocks
ticket 05... build... and swap the target when 07 lands"). ``cli.py`` skips the
outbox drain entirely, with a log line, when ``configured`` is ``False``.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from st_exporter.url_validation import validate_outbox_url


class TradeRatedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADERATED_")

    machine_token: str | None = Field(default=None, repr=False)
    outbox_base_url: str | None = None

    # A SECOND token, not the same one. TrueQuote mints machine tokens per
    # company AND per scope, and rejects a token presented to the wrong scope
    # with 401 (`authenticateMachineToken`, machine-token.ts:153 — `data.scope
    # !== scope`). `machine_token` above is the `booking_outbox` token; this is
    # the `image_upload` one, issued from the same panel as a separate value.
    image_token: str | None = Field(default=None, repr=False)

    # The image route is TRUEQUOTE'S (`/api/outbox/pricebook-image`), and it is
    # reached under TRUEQUOTE'S base url — not TradeRated's, which is a Supabase
    # Functions origin serving the `crm-outbox` edge function and has no such
    # route. Shipping the image lane under `TRADERATED_OUTBOX_BASE_URL` was
    # therefore only ever going to work for a contractor who had set that secret
    # to TrueQuote's host.
    #
    # The two correctly-named secrets below WIN when set; the two above remain
    # accepted so a connector already deployed with the old names keeps working.
    # Widening, never narrowing: a connector pinned in a contractor's own
    # repository cannot be updated on our schedule.
    truequote_outbox_url: str | None = Field(default=None, validation_alias="TRUEQUOTE_OUTBOX_URL")
    truequote_image_token: str | None = Field(
        default=None, repr=False, validation_alias="TRUEQUOTE_IMAGE_TOKEN"
    )

    # Both URL fields are checked the moment they are loaded, before anything
    # can build a client on them — the Machine Token is a bearer credential, and
    # `image_base_url` below hands whichever of these is set straight to
    # `TrueQuoteImageClient`. Empty/unset stays valid: that is an unbought
    # product, not a misconfigured one. The error is not a ValueError, so
    # pydantic re-raises it unwrapped and the operator gets one clean sentence.
    @field_validator("outbox_base_url")
    @classmethod
    def _check_outbox_base_url(cls, value: str | None) -> str | None:
        return validate_outbox_url(value, "TRADERATED_OUTBOX_BASE_URL")

    @field_validator("truequote_outbox_url")
    @classmethod
    def _check_truequote_outbox_url(cls, value: str | None) -> str | None:
        return validate_outbox_url(value, "TRUEQUOTE_OUTBOX_URL")

    @property
    def image_base_url(self) -> str | None:
        """Where the pricebook image route lives. TrueQuote's host, if named."""
        return self.truequote_outbox_url or self.outbox_base_url

    @property
    def image_upload_token(self) -> str | None:
        """The `image_upload`-scoped machine token, under either spelling."""
        return self.truequote_image_token or self.image_token

    @property
    def configured(self) -> bool:
        """True only when BOTH values are present *and* non-empty.

        Must be a truthiness check, not ``is not None``: GitHub Actions sets an
        ``env:`` entry mapped to an unset secret to the EMPTY STRING, not to
        nothing at all. Pydantic then loads ``machine_token=""`` /
        ``outbox_base_url=""`` — both non-None — and an ``is not None`` check
        would report the outbox as configured, so the drain would build a client
        against the empty base URL and crash with ``httpx.UnsupportedProtocol``
        on every scheduled run until ticket 07 issues real secrets.
        """
        return bool(self.machine_token) and bool(self.outbox_base_url)

    @property
    def images_configured(self) -> bool:
        """True when the image-upload lane has both of its values.

        Truthiness for the same reason ``configured`` uses it: an Actions
        ``env:`` entry mapped to an unset secret arrives as the empty string.
        The base url is shared with TrueQuote's BOOKING outbox lane — one
        TrueQuote deployment, three routes under it (`/pricebook-image`,
        `/booking/claim`, `/booking/result`) — but NOT with TradeRated's, which
        is a different app on a different host entirely.
        """
        return bool(self.image_upload_token) and bool(self.image_base_url)

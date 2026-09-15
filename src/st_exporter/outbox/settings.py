"""Which outbox lanes this contractor bought, read from the environment only.

One pair of secrets per product (ticket 12's table):

    TRADERATED_MACHINE_TOKEN    TRADERATED_OUTBOX_URL
    TRUEQUOTE_MACHINE_TOKEN     TRUEQUOTE_OUTBOX_URL
    PROFITWIZARD_MACHINE_TOKEN  PROFITWIZARD_OUTBOX_URL

A lane runs only when **both** of its values are present and non-empty — the
rule the TradeRated lane already used, generalised. An unbought product is
simply absent, so nothing has to know what a contractor purchased.

Truthiness, not ``is not None``: GitHub Actions maps an ``env:`` entry for an
unset secret to the EMPTY STRING, so every value here arrives as ``""`` rather
than as nothing at all, and an ``is not None`` check would report every lane as
configured and build httpx clients on empty base URLs.

Each lane's **paths are configuration, not a constant**: the three apps landed
on three different shapes (see ``routes.py``), and ``{PREFIX}_OUTBOX_CLAIM_PATH``
/ ``{PREFIX}_OUTBOX_RESULT_PATH`` can override either without an exporter
release — which matters because a connector pinned in a contractor's own
repository cannot be updated on our schedule.

``{PREFIX}_OUTBOX_BASE_URL`` is accepted as an equal spelling of
``{PREFIX}_OUTBOX_URL``. The reusable workflow has passed the TradeRated lane's
URL under the ``_BASE_URL`` name since 0.2.0 and connector repos deployed in
contractors' own repositories cannot be updated on our schedule, so the name is
widened rather than replaced. ``_OUTBOX_URL`` wins if a repo somehow sets both.

Deliberately plain ``os.environ`` rather than pydantic-settings: three products
x two spellings x two optional path overrides is a lookup table, and pydantic's
``env_prefix`` is fixed per class, so expressing it there would need one class
per product for no gain. ``ExporterSettings``'s no-``.env``-fallback rule is
preserved for the same reason it exists — a GitHub Actions runner has none.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from st_exporter.outbox.routes import (
    PROFITWIZARD_ROUTES,
    TRADERATED_ROUTES,
    TRUEQUOTE_ROUTES,
    LaneRoutes,
)
from st_exporter.url_validation import validate_outbox_url

# Product keys. These are also the ledger's product namespace and the prefix on
# every log line, so they are lower-case and stable — renaming one would orphan
# every `_outbox_ledger` row written under the old name.
TRADERATED = "traderated"
TRUEQUOTE = "truequote"
PROFITWIZARD = "profitwizard"

_ENV_PREFIXES = {
    TRADERATED: "TRADERATED",
    TRUEQUOTE: "TRUEQUOTE",
    PROFITWIZARD: "PROFITWIZARD",
}

# Ordered so a run's log reads the same way every time, and so the oldest,
# best-understood lane is drained first when several are configured.
PRODUCTS = (TRADERATED, TRUEQUOTE, PROFITWIZARD)


# Every product's known route shape. The default when nothing overrides it.
_DEFAULT_ROUTES: dict[str, LaneRoutes] = {
    TRADERATED: TRADERATED_ROUTES,
    TRUEQUOTE: TRUEQUOTE_ROUTES,
    PROFITWIZARD: PROFITWIZARD_ROUTES,
}


@dataclass(frozen=True)
class LaneCredentials:
    """One product's outbox credentials. Only ever constructed when both are set."""

    product: str
    base_url: str
    machine_token: str
    routes: LaneRoutes

    def __repr__(self) -> str:
        """Never print the token. A run that logs its settings must not leak it."""
        return (
            f"LaneCredentials(product={self.product!r}, base_url={self.base_url!r}, "
            f"machine_token=<redacted>, routes={self.routes!r})"
        )


def load_lane_credentials(
    product: str, env: Mapping[str, str] | None = None
) -> LaneCredentials | None:
    """``product``'s credentials, or ``None`` when it is not fully configured."""
    environ = os.environ if env is None else env
    prefix = _ENV_PREFIXES[product]

    token = (environ.get(f"{prefix}_MACHINE_TOKEN") or "").strip()
    url_var = f"{prefix}_OUTBOX_URL"
    raw_base_url = environ.get(url_var) or ""
    if not raw_base_url.strip():
        url_var = f"{prefix}_OUTBOX_BASE_URL"
        raw_base_url = environ.get(url_var) or ""
    base_url = raw_base_url.strip()
    if not token or not base_url:
        return None

    # Only now, with a lane that would actually run: this base URL is about to
    # receive `Authorization: Bearer <machine token>` on every claim, so a value
    # we would not send a credential to must stop the run rather than be dialled.
    # `url_var` is the spelling this contractor actually set, so the error names
    # the secret they have to go and fix.
    validate_outbox_url(base_url, url_var)

    default = _DEFAULT_ROUTES[product]
    return LaneCredentials(
        product=product,
        base_url=base_url,
        machine_token=token,
        routes=LaneRoutes(
            claim_path=(environ.get(f"{prefix}_OUTBOX_CLAIM_PATH") or "").strip()
            or default.claim_path,
            result_path=(environ.get(f"{prefix}_OUTBOX_RESULT_PATH") or "").strip()
            or default.result_path,
            token_scope=default.token_scope,
        ),
    )


def load_all_lane_credentials(
    env: Mapping[str, str] | None = None,
) -> list[LaneCredentials]:
    """Every configured lane, in :data:`PRODUCTS` order. Empty when none is."""
    found = (load_lane_credentials(product, env) for product in PRODUCTS)
    return [credentials for credentials in found if credentials is not None]

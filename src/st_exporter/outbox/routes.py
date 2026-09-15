"""Where each product's outbox lives, and under which token scope.

**Per-lane configuration, not a global constant.** The three apps landed on
three different path shapes, and all three are correct:

    TradeRated    GET|POST {base}/crm-outbox          POST {base}/crm-outbox/{item_id}/result
    TrueQuote     POST     {base}/booking/claim       POST {base}/booking/result
    Profit Wizard POST     {base}/claim               POST {base}/result

They are not a mistake to reconcile. TradeRated's base is a Supabase Functions
origin (``https://<ref>.supabase.co/functions/v1``) where the whole edge
function is one route and the id is a path segment; TrueQuote's and Profit
Wizard's are Next.js route handlers under ``https://<host>/api/outbox``, where
TrueQuote's booking queue is one of several things under that prefix
(``/pricebook-image`` is another, which is why its routes are a level deeper)
and Profit Wizard's is the only one. Forcing them onto one shape would mean
asking two shipped apps to move a URL for our convenience.

The **token scope** is per-lane too, and the vocabularies do not even agree with
each other: TradeRated mints ``crm_outbox``, TrueQuote ``booking_outbox`` and
``image_upload``, Profit Wizard ``servicetitan_outbox``. Every one of them
answers a token presented at the wrong scope with a flat 401, so there is no
"one token per app" and no fallback to try. The scope is recorded here because
it is the first thing to check when a lane starts 401ing, not because the
exporter sends it — the token itself carries it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LaneRoutes:
    """One product's paths, relative to its own ``*_OUTBOX_URL`` base."""

    claim_path: str
    # A template, because TradeRated puts the item id in the path and the other
    # two put it in the body. ``.format(item_id=...)`` is applied either way; a
    # template with no placeholder is simply returned unchanged.
    result_path: str
    # The Machine Token scope this lane's secret must have been minted at. Not
    # sent on the wire — the token carries it — but the 401 it causes is
    # otherwise indistinguishable from a revoked or unknown token.
    token_scope: str

    def result_path_for(self, item_id: str) -> str:
        return self.result_path.format(item_id=item_id)


TRADERATED_ROUTES = LaneRoutes(
    claim_path="/crm-outbox",
    result_path="/crm-outbox/{item_id}/result",
    token_scope="crm_outbox",
)

TRUEQUOTE_ROUTES = LaneRoutes(
    claim_path="/booking/claim",
    result_path="/booking/result",
    token_scope="booking_outbox",
)

PROFITWIZARD_ROUTES = LaneRoutes(
    claim_path="/claim",
    result_path="/result",
    token_scope="servicetitan_outbox",
)

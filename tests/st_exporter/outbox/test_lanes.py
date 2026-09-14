"""Tests for build_lanes — which products become drainable lanes, and why not."""

from __future__ import annotations

from unittest.mock import MagicMock

from st_exporter.outbox.lanes import (
    ProfitWizardLane,
    TradeRatedLane,
    TrueQuoteLane,
    build_lanes,
    close_lanes,
)
from st_exporter.outbox.settings import (
    PROFITWIZARD,
    TRADERATED,
    TRUEQUOTE,
    load_lane_credentials,
)


def _credentials(product: str, **env_extra):
    prefix = product.upper() if product != PROFITWIZARD else "PROFITWIZARD"
    env = {
        f"{prefix}_MACHINE_TOKEN": "tok",
        f"{prefix}_OUTBOX_URL": "https://example.test",
    }
    env.update(env_extra)
    credentials = load_lane_credentials(product, env)
    assert credentials is not None
    return credentials


class TestBuildLanes:
    def test_traderated_gets_the_crm_outbox_shape(self) -> None:
        lanes, skipped = build_lanes([_credentials(TRADERATED)], MagicMock())
        assert skipped == []
        assert isinstance(lanes[0], TradeRatedLane)
        assert lanes[0].product == TRADERATED
        close_lanes(lanes)

    def test_truequote_gets_the_booking_shape(self) -> None:
        lanes, skipped = build_lanes([_credentials(TRUEQUOTE)], MagicMock())
        assert skipped == []
        assert isinstance(lanes[0], TrueQuoteLane)
        assert lanes[0].product == TRUEQUOTE
        close_lanes(lanes)

    def test_two_bought_products_build_two_lanes(self) -> None:
        lanes, skipped = build_lanes(
            [_credentials(TRADERATED), _credentials(TRUEQUOTE)], MagicMock()
        )
        assert [lane.product for lane in lanes] == [TRADERATED, TRUEQUOTE]
        assert skipped == []
        close_lanes(lanes)

    def test_nothing_configured_builds_nothing(self) -> None:
        assert build_lanes([], MagicMock()) == ([], [])


class TestProfitWizardLane:
    """Profit Wizard's outbox is live: `POST {base}/claim` + `POST {base}/result`,
    Machine Token scope `servicetitan_outbox`."""

    def test_profitwizard_gets_its_own_shape(self) -> None:
        lanes, skipped = build_lanes([_credentials(PROFITWIZARD)], MagicMock())
        assert skipped == []
        assert isinstance(lanes[0], ProfitWizardLane)
        assert lanes[0].product == PROFITWIZARD
        close_lanes(lanes)

    def test_all_three_products_drain_side_by_side(self) -> None:
        lanes, skipped = build_lanes(
            [_credentials(TRADERATED), _credentials(TRUEQUOTE), _credentials(PROFITWIZARD)],
            MagicMock(),
        )
        assert skipped == []
        assert [lane.product for lane in lanes] == [TRADERATED, TRUEQUOTE, PROFITWIZARD]
        close_lanes(lanes)


class TestPerLaneRoutes:
    """Three apps, three path shapes, three token scopes — none of it global."""

    def test_each_product_carries_its_own_paths_and_scope(self) -> None:
        assert _credentials(TRADERATED).routes.claim_path == "/crm-outbox"
        assert _credentials(TRADERATED).routes.result_path_for("abc") == "/crm-outbox/abc/result"
        assert _credentials(TRADERATED).routes.token_scope == "crm_outbox"

        assert _credentials(TRUEQUOTE).routes.claim_path == "/booking/claim"
        assert _credentials(TRUEQUOTE).routes.result_path_for("abc") == "/booking/result"
        assert _credentials(TRUEQUOTE).routes.token_scope == "booking_outbox"

        assert _credentials(PROFITWIZARD).routes.claim_path == "/claim"
        assert _credentials(PROFITWIZARD).routes.result_path_for("abc") == "/result"
        assert _credentials(PROFITWIZARD).routes.token_scope == "servicetitan_outbox"

    def test_a_path_can_be_overridden_without_an_exporter_release(self) -> None:
        """A connector pinned in a contractor's own repository cannot be updated
        on our schedule, so a moved route must be a secret, not a release."""
        credentials = _credentials(PROFITWIZARD, PROFITWIZARD_OUTBOX_CLAIM_PATH="/v2/claim")
        assert credentials.routes.claim_path == "/v2/claim"
        assert credentials.routes.result_path == "/result", "the other path is untouched"


class TestCloseLanes:
    def test_one_failing_close_does_not_skip_the_rest(self) -> None:
        exploding, healthy = MagicMock(product="a"), MagicMock(product="b")
        exploding.close.side_effect = RuntimeError("socket already gone")
        close_lanes([exploding, healthy])
        healthy.close.assert_called_once()

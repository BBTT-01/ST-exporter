"""Tests for lane credentials — which products this contractor bought."""

from __future__ import annotations

from st_exporter.outbox.settings import (
    PROFITWIZARD,
    TRADERATED,
    TRUEQUOTE,
    load_all_lane_credentials,
    load_lane_credentials,
)


class TestOneLane:
    def test_both_secrets_present_configures_the_lane(self) -> None:
        env = {"TRUEQUOTE_MACHINE_TOKEN": "tqm_x", "TRUEQUOTE_OUTBOX_URL": "https://tq/api/outbox"}
        credentials = load_lane_credentials(TRUEQUOTE, env)
        assert credentials is not None
        assert credentials.base_url == "https://tq/api/outbox"
        assert credentials.machine_token == "tqm_x"

    def test_a_missing_secret_leaves_the_lane_unconfigured(self) -> None:
        assert load_lane_credentials(TRUEQUOTE, {"TRUEQUOTE_MACHINE_TOKEN": "tqm_x"}) is None
        assert load_lane_credentials(TRUEQUOTE, {"TRUEQUOTE_OUTBOX_URL": "https://tq"}) is None

    def test_empty_strings_are_not_configured(self) -> None:
        """GitHub Actions maps an `env:` entry for an unset secret to `""`, not
        to nothing — the exact bug that once built an httpx client on an empty
        base URL and crashed every scheduled run."""
        env = {"PROFITWIZARD_MACHINE_TOKEN": "", "PROFITWIZARD_OUTBOX_URL": ""}
        assert load_lane_credentials(PROFITWIZARD, env) is None

    def test_whitespace_only_secrets_are_not_configured(self) -> None:
        env = {"TRADERATED_MACHINE_TOKEN": "  ", "TRADERATED_OUTBOX_URL": "\\n"}
        assert load_lane_credentials(TRADERATED, env) is None

    def test_the_legacy_base_url_spelling_is_still_accepted(self) -> None:
        """`TRADERATED_OUTBOX_BASE_URL` is what the reusable workflow has passed
        since 0.2.0. A connector pinned in a contractor's own repository cannot
        be updated on our schedule, so the name widens rather than moves."""
        env = {
            "TRADERATED_MACHINE_TOKEN": "trmt_x",
            "TRADERATED_OUTBOX_BASE_URL": "https://ref.supabase.co/functions/v1",
        }
        credentials = load_lane_credentials(TRADERATED, env)
        assert credentials is not None
        assert credentials.base_url == "https://ref.supabase.co/functions/v1"

    def test_the_ticket_spelling_wins_when_both_are_set(self) -> None:
        env = {
            "TRADERATED_MACHINE_TOKEN": "trmt_x",
            "TRADERATED_OUTBOX_URL": "https://new",
            "TRADERATED_OUTBOX_BASE_URL": "https://old",
        }
        assert load_lane_credentials(TRADERATED, env).base_url == "https://new"

    def test_the_token_is_never_in_the_repr(self) -> None:
        env = {"TRUEQUOTE_MACHINE_TOKEN": "tqm_supersecret", "TRUEQUOTE_OUTBOX_URL": "https://tq"}
        assert "tqm_supersecret" not in repr(load_lane_credentials(TRUEQUOTE, env))


class TestAllLanes:
    def test_no_secrets_is_no_lanes(self) -> None:
        assert load_all_lane_credentials({}) == []

    def test_only_traderated_gives_exactly_one_lane(self) -> None:
        env = {"TRADERATED_MACHINE_TOKEN": "t", "TRADERATED_OUTBOX_URL": "https://tr"}
        assert [c.product for c in load_all_lane_credentials(env)] == [TRADERATED]

    def test_traderated_and_truequote_give_two_lanes_in_a_stable_order(self) -> None:
        env = {
            "TRUEQUOTE_MACHINE_TOKEN": "q",
            "TRUEQUOTE_OUTBOX_URL": "https://tq",
            "TRADERATED_MACHINE_TOKEN": "t",
            "TRADERATED_OUTBOX_URL": "https://tr",
        }
        assert [c.product for c in load_all_lane_credentials(env)] == [TRADERATED, TRUEQUOTE]

    def test_all_three_products_give_three_lanes(self) -> None:
        env = {
            f"{prefix}_{suffix}": value
            for prefix in ("TRADERATED", "TRUEQUOTE", "PROFITWIZARD")
            for suffix, value in (("MACHINE_TOKEN", "t"), ("OUTBOX_URL", "https://x"))
        }
        assert [c.product for c in load_all_lane_credentials(env)] == [
            TRADERATED,
            TRUEQUOTE,
            PROFITWIZARD,
        ]

    def test_an_unbought_product_is_simply_absent(self) -> None:
        env = {
            "TRUEQUOTE_MACHINE_TOKEN": "q",
            "TRUEQUOTE_OUTBOX_URL": "https://tq",
            "PROFITWIZARD_MACHINE_TOKEN": "",
            "PROFITWIZARD_OUTBOX_URL": "",
        }
        assert [c.product for c in load_all_lane_credentials(env)] == [TRUEQUOTE]

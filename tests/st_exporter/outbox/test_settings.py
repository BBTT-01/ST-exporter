"""Tests for lane credentials — which products this contractor bought."""

from __future__ import annotations

import pytest

from st_exporter.outbox.settings import (
    PROFITWIZARD,
    TRADERATED,
    TRUEQUOTE,
    load_all_lane_credentials,
    load_lane_credentials,
)
from st_exporter.url_validation import InvalidOutboxUrlError


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


class TestUrlValidation:
    """A lane's base URL receives ``Authorization: Bearer <machine token>`` on
    every claim, so a value we would not send a credential to must stop the run
    rather than be dialled."""

    def test_https_is_accepted(self) -> None:
        env = {
            "PROFITWIZARD_MACHINE_TOKEN": "pw_x",
            "PROFITWIZARD_OUTBOX_URL": "https://pw.example.com/api/outbox",
        }
        credentials = load_lane_credentials(PROFITWIZARD, env)
        assert credentials is not None
        assert credentials.base_url == "https://pw.example.com/api/outbox"

    def test_http_is_refused(self) -> None:
        env = {
            "PROFITWIZARD_MACHINE_TOKEN": "pw_x",
            "PROFITWIZARD_OUTBOX_URL": "http://pw.example.com/api/outbox",
        }
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            load_lane_credentials(PROFITWIZARD, env)
        assert "unencrypted" in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_url",
        [
            "pw.example.com",
            "https://",
            "https://user:pass@pw.example.com",
            "https://pw.example.com?x=1",
            "https://pw.example.com#frag",
        ],
    )
    def test_malformed_urls_are_refused(self, bad_url: str) -> None:
        env = {"TRUEQUOTE_MACHINE_TOKEN": "tq_x", "TRUEQUOTE_OUTBOX_URL": bad_url}
        with pytest.raises(InvalidOutboxUrlError):
            load_lane_credentials(TRUEQUOTE, env)

    def test_the_error_names_the_spelling_the_contractor_actually_set(self) -> None:
        """Both spellings are accepted, so the message must name the one that is
        set — telling an operator to fix a secret they never created is useless."""
        env = {
            "TRADERATED_MACHINE_TOKEN": "tr_x",
            "TRADERATED_OUTBOX_BASE_URL": "http://tr.example.com",
        }
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            load_lane_credentials(TRADERATED, env)
        assert excinfo.value.env_var == "TRADERATED_OUTBOX_BASE_URL"
        assert "TRADERATED_OUTBOX_URL is" not in str(excinfo.value)

    def test_an_unconfigured_lane_is_never_validated(self) -> None:
        """An unbought product has no URL at all — that is absence, not a bad
        value, and it must stay a quiet ``None``."""
        assert load_lane_credentials(PROFITWIZARD, {}) is None
        assert (
            load_lane_credentials(
                PROFITWIZARD,
                {"PROFITWIZARD_MACHINE_TOKEN": "", "PROFITWIZARD_OUTBOX_URL": ""},
            )
            is None
        )

    def test_a_good_lane_beside_absent_products_loads_cleanly(self) -> None:
        env = {
            "TRUEQUOTE_MACHINE_TOKEN": "tq_x",
            "TRUEQUOTE_OUTBOX_URL": "https://tq.example.com",
            "TRADERATED_MACHINE_TOKEN": "",
            "TRADERATED_OUTBOX_URL": "",
            "PROFITWIZARD_MACHINE_TOKEN": "",
            "PROFITWIZARD_OUTBOX_URL": "",
        }
        assert [c.product for c in load_all_lane_credentials(env)] == [TRUEQUOTE]

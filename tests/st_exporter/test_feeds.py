"""Tests for parse_feeds — the --feeds CLI option's validation."""

from __future__ import annotations

import pytest

from st_cli.exceptions import ConfigError
from st_exporter.run import DEFAULT_FEEDS, parse_feeds


class TestParseFeeds:
    def test_both_feeds(self) -> None:
        assert parse_feeds("jobs,technicians") == {"jobs", "technicians"}

    def test_single_feed(self) -> None:
        assert parse_feeds("jobs") == {"jobs"}

    def test_whitespace_and_trailing_comma_tolerated(self) -> None:
        assert parse_feeds(" jobs , technicians, ") == {"jobs", "technicians"}

    def test_default_feeds_constant_is_both(self) -> None:
        assert DEFAULT_FEEDS == {"jobs", "technicians"}

    def test_pricebook_is_a_valid_feed(self) -> None:
        assert parse_feeds("pricebook") == {"pricebook"}

    def test_pricebook_is_not_a_default_feed(self) -> None:
        # Catalogue cadence, not the ~5-minute jobs cadence.
        assert "pricebook" not in DEFAULT_FEEDS

    def test_financial_is_a_valid_feed(self) -> None:
        assert parse_feeds("financial") == {"financial"}

    def test_financial_is_not_a_default_feed(self) -> None:
        # Six-hourly cadence, and its job-costing half runs a ServiceTitan report
        # throttled to roughly one run per minute per tenant.
        assert "financial" not in DEFAULT_FEEDS

    def test_unknown_feed_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="unknown"):
            parse_feeds("jobs,price-book")

    def test_empty_string_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="at least one"):
            parse_feeds("")

    def test_only_commas_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="at least one"):
            parse_feeds(" , ,")

"""Tests for parse_feeds — the --feeds CLI option's validation."""

from __future__ import annotations

import pytest

from st_cli.exceptions import ConfigError
from st_exporter.run import DEFAULT_FEEDS, EXPORT_FEEDS, IMAGES_FEED, parse_feeds


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


class TestTheImagesFeed:
    """`images` is a feed of its own, not a flag on `pricebook`.

    It used to be `--upload-images` inside the pricebook run, sharing that
    feed's ten-minute job. On `BBTT-01/tr-doorservpro` (~7,191 assets) the
    runner SIGKILLed the download pass at 10m35s with 0 images uploaded — run
    35130164187 — and failed the hourly pricebook export with it.
    """

    def test_images_is_a_valid_feed(self) -> None:
        assert parse_feeds("images") == {"images"}

    def test_images_is_not_an_export_feed(self) -> None:
        """It writes no tab and no `_meta` row, which is what lets the caller
        give its job a concurrency lock of its own. If it ever joins
        EXPORT_FEEDS it must also go back on the shared lock."""
        assert IMAGES_FEED not in EXPORT_FEEDS

    def test_images_is_not_a_default_feed(self) -> None:
        """Its own cadence, like pricebook and financial — and unlike them it
        costs image bandwidth, so it must never ride along with jobs."""
        assert IMAGES_FEED not in DEFAULT_FEEDS

    def test_images_combines_with_the_export_feeds(self) -> None:
        """Nothing forbids it; the caller workflow simply does not do it,
        because that would put the job back on the shared export lock."""
        assert parse_feeds("pricebook,images") == {"pricebook", "images"}

    def test_the_error_message_names_images(self) -> None:
        """A contractor who mistypes it must be told the feed exists."""
        with pytest.raises(ConfigError, match="images"):
            parse_feeds("imagez")

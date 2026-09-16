"""Tests for the conditional-request helpers and, above all, the MEASUREMENT.

Nobody has seen ServiceTitan answer a conditional request. These tests pin the
two things that must hold whatever the answer turns out to be: no validator
means a plain GET (today's behaviour), and every outcome is counted in terms an
operator can read off one line of a workflow log.
"""

from __future__ import annotations

import pytest

from st_exporter.images.conditional import ConditionalReport, Validators, looks_signed


class TestValidators:
    def test_nothing_stored_sends_no_headers_at_all(self) -> None:
        """The fallback, and the default: an unconditional GET."""
        blank = Validators()
        assert blank.usable is False
        assert blank.headers() == {}

    def test_both_are_sent_when_both_are_known(self) -> None:
        """We do not know which of the two ServiceTitan implements, if either."""
        both = Validators(etag='"v1"', last_modified="Mon, 14 Sep 2026 00:00:00 GMT")
        assert both.headers() == {
            "If-None-Match": '"v1"',
            "If-Modified-Since": "Mon, 14 Sep 2026 00:00:00 GMT",
        }

    def test_an_etag_alone_is_enough(self) -> None:
        assert Validators(etag='"v1"').headers() == {"If-None-Match": '"v1"'}

    def test_a_last_modified_alone_is_enough(self) -> None:
        assert Validators(last_modified="x").headers() == {"If-Modified-Since": "x"}


class TestLooksSigned:
    """A signed url defeats conditional fetching twice over, and cheaply
    detecting it is the difference between "this feature does not work" and
    "this feature cannot work here"."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.example.com/a.jpg?X-Amz-Signature=deadbeef",
            "https://cdn.example.com/a.jpg?x-amz-credential=k&X-Amz-Date=1",
            "https://blob.core.windows.net/a.jpg?sv=2020&sig=abc&se=2026",
            "https://storage.googleapis.com/a.jpg?X-Goog-Signature=abc",
            "https://cdn.example.com/a.jpg?token=abc",
        ],
    )
    def test_signed_urls_are_recognised(self, url: str) -> None:
        assert looks_signed(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.example.com/a.jpg",
            # A plain cache-buster is NOT a signature: treating every query
            # string as signed would report a false alarm on most CDNs.
            "https://cdn.example.com/a.jpg?v=3",
            "Images/Pricebook/9f2c-uuid.jpg",
            "",
        ],
    )
    def test_ordinary_urls_are_not(self, url: str) -> None:
        assert looks_signed(url) is False


class TestTheVerdict:
    """One English sentence, for a reader six months from now who has never
    opened this module."""

    def test_nothing_fetched_claims_nothing(self) -> None:
        assert "nothing was learned" in ConditionalReport().verdict()

    def test_no_validators_anywhere_is_stated_plainly(self) -> None:
        report = ConditionalReport()
        for _ in range(3):
            report.observe_response(
                conditional=False, status_code=200, etag=None, last_modified=None
            )
        assert "NO validator" in report.verdict()
        assert report.validators_absent == 3

    def test_validators_stored_but_not_yet_tested(self) -> None:
        report = ConditionalReport()
        report.observe_response(conditional=False, status_code=200, etag='"v1"', last_modified=None)
        assert "the next run is the one that proves 304s" in report.verdict()

    def test_conditional_requests_all_ignored(self) -> None:
        report = ConditionalReport()
        report.observe_response(conditional=True, status_code=200, etag='"v1"', last_modified=None)
        assert "NOT ONE 304" in report.verdict()
        assert report.conditional_missed == 1

    def test_conditional_requests_working_reports_the_share(self) -> None:
        report = ConditionalReport()
        for _ in range(3):
            report.observe_response(
                conditional=True, status_code=304, etag=None, last_modified=None
            )
        report.observe_response(conditional=True, status_code=200, etag='"v2"', last_modified=None)
        assert "conditional requests WORK: 3/4 (75%)" in report.verdict()

    def test_signed_urls_are_offered_as_the_explanation(self) -> None:
        report = ConditionalReport()
        report.observe_asset(source_url="https://cdn.example.com/a.jpg?sig=x", has_asset_id=False)
        report.observe_response(conditional=False, status_code=200, etag=None, last_modified=None)
        assert "look signed" in report.verdict()
        assert (report.signed_urls, report.unstable_refs) == (1, 1)

    def test_a_signed_url_with_a_stable_asset_id_is_not_an_unstable_ref(self) -> None:
        """The url moves, but the ledger keys off `asset.id`, so the previous
        run's entry is still FOUND — only the validator is wasted."""
        report = ConditionalReport()
        report.observe_asset(source_url="https://cdn.example.com/a.jpg?sig=x", has_asset_id=True)
        assert (report.signed_urls, report.unstable_refs) == (1, 0)

    def test_a_weak_etag_is_counted_as_such(self) -> None:
        report = ConditionalReport()
        report.observe_response(
            conditional=False, status_code=200, etag='W/"v1"', last_modified=None
        )
        assert report.weak_etags == 1

    def test_the_log_line_names_every_counter(self) -> None:
        fields = ConditionalReport().as_log_fields()
        for name in (
            "fetches",
            "conditional_sent",
            "not_modified",
            "conditional_missed",
            "validators_present",
            "validators_absent",
            "weak_etags",
            "signed_urls",
            "unstable_refs",
        ):
            assert f"{name}=" in fields

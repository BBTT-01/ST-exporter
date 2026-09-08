"""Tests for referral-campaign resolve-or-create.

`POST crm/leads` is refused without a `campaignId`, and TradeRated's Outbox payload
carries none — so these cover the exporter supplying one itself, including the first
run on a tenant where the campaign does not exist yet.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_cli.config import Settings
from st_exporter.outbox.campaign import (
    REFERRAL_CAMPAIGN_NAME,
    CampaignResolutionError,
    ReferralCampaign,
)
from tests.st_exporter.conftest import mock_auth_token


def _campaigns_url(st_settings: Settings) -> str:
    return f"{st_settings.api_base}/marketing/v2/tenant/{st_settings.tenant_id}/campaigns"


def _units_url(st_settings: Settings) -> str:
    return f"{st_settings.api_base}/settings/v2/tenant/{st_settings.tenant_id}/business-units"


def _categories_url(st_settings: Settings) -> str:
    return f"{st_settings.api_base}/marketing/v2/tenant/{st_settings.tenant_id}/categories"


def _page(records: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"data": records, "hasMore": False})


@pytest.fixture(autouse=True)
def _no_pinned_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    # The overrides are read from the process environment, so a value left set by the
    # host would silently skip the resolution these tests exist to cover.
    monkeypatch.delenv("TRADERATED_CAMPAIGN_BUSINESS_UNIT_ID", raising=False)
    monkeypatch.delenv("TRADERATED_CAMPAIGN_CATEGORY_ID", raising=False)


class TestExistingCampaign:
    @respx.mock
    def test_finds_the_campaign_by_name(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"id": 1, "name": "Truck Wraps"},
                        {"id": 42, "name": REFERRAL_CAMPAIGN_NAME},
                    ],
                    "hasMore": False,
                },
            )
        )
        created = respx.post(_campaigns_url(st_settings))

        client = ServiceTitanClient(st_settings)
        try:
            assert ReferralCampaign(client).campaign_id() == 42
        finally:
            client.close()

        # Creating a second campaign on a tenant that already has one would split the
        # customer's referral revenue across two rows in their own reporting.
        assert not created.called

    @respx.mock
    def test_matching_ignores_case(self, st_settings: Settings) -> None:
        # A customer or an earlier run may have created it with different casing; a second
        # campaign with the same name is worse than reusing theirs.
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": 7, "name": "traderated referrals"}], "hasMore": False},
            )
        )
        created = respx.post(_campaigns_url(st_settings))

        client = ServiceTitanClient(st_settings)
        try:
            assert ReferralCampaign(client).campaign_id() == 7
        finally:
            client.close()
        assert not created.called

    @respx.mock
    def test_finds_it_on_a_later_page(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(
            side_effect=[
                httpx.Response(200, json={"data": [{"id": 1, "name": "GMB"}], "hasMore": True}),
                httpx.Response(
                    200,
                    json={"data": [{"id": 99, "name": REFERRAL_CAMPAIGN_NAME}], "hasMore": False},
                ),
            ]
        )

        client = ServiceTitanClient(st_settings)
        try:
            assert ReferralCampaign(client).campaign_id() == 99
        finally:
            client.close()


class TestFirstRun:
    @respx.mock
    def test_creates_the_campaign_when_absent(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": 1, "name": "Truck Wraps"}], "hasMore": False}
            )
        )
        respx.get(_units_url(st_settings)).mock(
            return_value=_page([{"id": 9, "active": True}, {"id": 4, "active": True}])
        )
        respx.get(_categories_url(st_settings)).mock(
            return_value=_page([{"id": 31, "active": True}, {"id": 77, "active": True}])
        )
        created = respx.post(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(200, json={"id": 500})
        )

        client = ServiceTitanClient(st_settings)
        try:
            assert ReferralCampaign(client).campaign_id() == 500
        finally:
            client.close()

        # ServiceTitan refused a name-only body on 2026-09-08 with `categoryId` and
        # `businessUnitId` both reported missing; both are now resolved from the tenant.
        assert json.loads(created.calls.last.request.content) == {
            "name": REFERRAL_CAMPAIGN_NAME,
            "active": True,
            "businessUnitId": 4,
            "categoryId": 31,
        }

    @respx.mock
    def test_a_refused_create_names_the_campaign_in_the_error(self, st_settings: Settings) -> None:
        # The failure this replaces was an opaque `400: campaignId and summary required`.
        # If ServiceTitan wants more than a name, the drain must report which call failed.
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(200, json={"data": [], "hasMore": False})
        )
        respx.get(_units_url(st_settings)).mock(return_value=_page([{"id": 1}]))
        respx.get(_categories_url(st_settings)).mock(return_value=_page([{"id": 2}]))
        respx.post(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(400, json={"title": "dnis required"})
        )

        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(CampaignResolutionError, match=REFERRAL_CAMPAIGN_NAME):
                ReferralCampaign(client).campaign_id()
        finally:
            client.close()

    @respx.mock
    def test_an_accepted_create_with_no_id_is_an_error_not_a_none_campaign(
        self, st_settings: Settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(200, json={"data": [], "hasMore": False})
        )
        respx.get(_units_url(st_settings)).mock(return_value=_page([{"id": 1}]))
        respx.get(_categories_url(st_settings)).mock(return_value=_page([{"id": 2}]))
        respx.post(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(CampaignResolutionError, match="returned no id"):
                ReferralCampaign(client).campaign_id()
        finally:
            client.close()


class TestCaching:
    @respx.mock
    def test_resolves_once_and_reuses_the_id(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        listed = respx.get(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": 8, "name": REFERRAL_CAMPAIGN_NAME}], "hasMore": False}
            )
        )

        client = ServiceTitanClient(st_settings)
        try:
            resolver = ReferralCampaign(client)
            assert resolver.campaign_id() == 8
            assert resolver.campaign_id() == 8
            assert resolver.campaign_id() == 8
        finally:
            client.close()

        assert listed.call_count == 1

    @respx.mock
    def test_makes_no_marketing_call_until_asked(self, st_settings: Settings) -> None:
        # The drain builds a resolver for every batch, including batches with no referral
        # leads in them; those must not touch the marketing API at all.
        mock_auth_token(st_settings.auth_url)
        listed = respx.get(_campaigns_url(st_settings))

        client = ServiceTitanClient(st_settings)
        try:
            ReferralCampaign(client)
        finally:
            client.close()

        assert not listed.called


class TestRequiredIdResolution:
    @respx.mock
    def test_inactive_records_are_skipped_and_the_lowest_id_wins(
        self, st_settings: Settings
    ) -> None:
        # Deterministic by lowest active id, so two runs — and two customers — behave the
        # same way and the choice is reproducible from the run log.
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(return_value=_page([]))
        respx.get(_units_url(st_settings)).mock(
            return_value=_page([{"id": 2, "active": False}, {"id": 8, "active": True}])
        )
        respx.get(_categories_url(st_settings)).mock(
            return_value=_page([{"id": 3, "active": False}, {"id": 5, "active": True}])
        )
        created = respx.post(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(200, json={"id": 1})
        )

        client = ServiceTitanClient(st_settings)
        try:
            ReferralCampaign(client).campaign_id()
        finally:
            client.close()

        body = json.loads(created.calls.last.request.content)
        assert body["businessUnitId"] == 8
        assert body["categoryId"] == 5

    @respx.mock
    def test_a_missing_active_flag_counts_as_active(self, st_settings: Settings) -> None:
        # These list endpoints are not guaranteed to return `active`; dropping a record for
        # a missing flag would fail the campaign outright, which is strictly worse.
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(return_value=_page([]))
        respx.get(_units_url(st_settings)).mock(return_value=_page([{"id": 6}]))
        respx.get(_categories_url(st_settings)).mock(return_value=_page([{"id": 7}]))
        created = respx.post(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(200, json={"id": 1})
        )

        client = ServiceTitanClient(st_settings)
        try:
            ReferralCampaign(client).campaign_id()
        finally:
            client.close()

        body = json.loads(created.calls.last.request.content)
        assert body["businessUnitId"] == 6
        assert body["categoryId"] == 7

    @respx.mock
    def test_pinned_ids_win_and_skip_the_lookups(
        self, st_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A customer with several business units decides which one referral revenue lands
        # against; pinning must need no code change and no API call.
        monkeypatch.setenv("TRADERATED_CAMPAIGN_BUSINESS_UNIT_ID", "111")
        monkeypatch.setenv("TRADERATED_CAMPAIGN_CATEGORY_ID", "222")
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(return_value=_page([]))
        units = respx.get(_units_url(st_settings))
        cats = respx.get(_categories_url(st_settings))
        created = respx.post(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(200, json={"id": 1})
        )

        client = ServiceTitanClient(st_settings)
        try:
            ReferralCampaign(client).campaign_id()
        finally:
            client.close()

        body = json.loads(created.calls.last.request.content)
        assert body["businessUnitId"] == 111
        assert body["categoryId"] == 222
        assert not units.called
        assert not cats.called

    @respx.mock
    def test_a_non_numeric_pin_is_rejected_by_name(
        self, st_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRADERATED_CAMPAIGN_BUSINESS_UNIT_ID", "the big one")
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(return_value=_page([]))

        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(
                CampaignResolutionError, match="TRADERATED_CAMPAIGN_BUSINESS_UNIT_ID"
            ):
                ReferralCampaign(client).campaign_id()
        finally:
            client.close()

    @respx.mock
    def test_no_business_units_names_the_variable_to_set(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(return_value=_page([]))
        respx.get(_units_url(st_settings)).mock(return_value=_page([]))

        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(
                CampaignResolutionError, match="TRADERATED_CAMPAIGN_BUSINESS_UNIT_ID"
            ):
                ReferralCampaign(client).campaign_id()
        finally:
            client.close()

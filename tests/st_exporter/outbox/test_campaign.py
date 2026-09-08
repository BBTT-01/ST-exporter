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
        created = respx.post(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(200, json={"id": 500})
        )

        client = ServiceTitanClient(st_settings)
        try:
            assert ReferralCampaign(client).campaign_id() == 500
        finally:
            client.close()

        assert json.loads(created.calls.last.request.content) == {
            "name": REFERRAL_CAMPAIGN_NAME,
            "active": True,
        }

    @respx.mock
    def test_a_refused_create_names_the_campaign_in_the_error(self, st_settings: Settings) -> None:
        # The failure this replaces was an opaque `400: campaignId and summary required`.
        # If ServiceTitan wants more than a name, the drain must report which call failed.
        mock_auth_token(st_settings.auth_url)
        respx.get(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(200, json={"data": [], "hasMore": False})
        )
        respx.post(_campaigns_url(st_settings)).mock(
            return_value=httpx.Response(400, json={"title": "businessUnitId required"})
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

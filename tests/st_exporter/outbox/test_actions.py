"""Tests for outbox action dispatch — one function per Outbox kind."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_cli.config import Settings
from st_exporter.outbox.actions import UnsupportedOutboxKindError, perform_item
from st_exporter.outbox.campaign import REFERRAL_CAMPAIGN_NAME, ReferralCampaign
from st_exporter.outbox.client import OutboxItem
from tests.st_exporter.conftest import mock_auth_token


def _campaign_list_route(st_settings: Settings, records: list[dict]) -> respx.Route:
    return respx.get(
        f"{st_settings.api_base}/marketing/v2/tenant/{st_settings.tenant_id}/campaigns"
    ).mock(return_value=httpx.Response(200, json={"data": records, "hasMore": False}))


class TestReferralLead:
    @respx.mock
    def test_creates_a_crm_lead_and_returns_its_id(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _campaign_list_route(st_settings, [{"id": 77, "name": REFERRAL_CAMPAIGN_NAME}])
        route = respx.post(
            f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}/leads"
        ).mock(return_value=httpx.Response(200, json={"id": 999}))

        client = ServiceTitanClient(st_settings)
        try:
            item = OutboxItem(
                id="1", idempotency_key="key-1", kind="referral_lead", payload={"name": "Jane"}
            )
            st_id = perform_item(client, item)
        finally:
            client.close()

        assert st_id == "999"
        body = json.loads(route.calls.last.request.content)
        # The two fields ServiceTitan rejected the real 2026-09-08 attempt for.
        assert body["campaignId"] == 77
        assert body["summary"] == "Referral from TradeRated for Jane"
        # Everything TradeRated sent is still forwarded untouched.
        assert body["name"] == "Jane"

    @respx.mock
    def test_summary_carries_the_contact_details_a_human_needs(self, st_settings: Settings) -> None:
        # `summary` is the only field on a bare lead that office staff actually read, so
        # losing the name and phone would make the lead unworkable.
        mock_auth_token(st_settings.auth_url)
        _campaign_list_route(st_settings, [{"id": 77, "name": REFERRAL_CAMPAIGN_NAME}])
        route = respx.post(
            f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}/leads"
        ).mock(return_value=httpx.Response(200, json={"id": 1}))

        client = ServiceTitanClient(st_settings)
        try:
            perform_item(
                client,
                OutboxItem(
                    id="1",
                    idempotency_key="key-1",
                    kind="referral_lead",
                    payload={
                        "name": "Akilans test friend",
                        "phone": "555-0100",
                        "email": "fidelisakilan@gmail.com",
                        "referred_by": "Derrick Harman",
                        "notes": "wants a quote for two doors",
                    },
                ),
            )
        finally:
            client.close()

        summary = json.loads(route.calls.last.request.content)["summary"]
        for expected in (
            "Akilans test friend",
            "Derrick Harman",
            "555-0100",
            "fidelisakilan@gmail.com",
            "wants a quote for two doors",
        ):
            assert expected in summary

    @respx.mock
    def test_a_campaign_id_from_traderated_is_not_overridden(self, st_settings: Settings) -> None:
        # If TradeRated ever starts choosing the campaign, its choice must win — and no
        # campaign lookup should happen at all.
        mock_auth_token(st_settings.auth_url)
        listed = _campaign_list_route(st_settings, [{"id": 77, "name": REFERRAL_CAMPAIGN_NAME}])
        route = respx.post(
            f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}/leads"
        ).mock(return_value=httpx.Response(200, json={"id": 5}))

        client = ServiceTitanClient(st_settings)
        try:
            perform_item(
                client,
                OutboxItem(
                    id="1",
                    idempotency_key="key-1",
                    kind="referral_lead",
                    payload={"campaignId": 4242, "summary": "chosen upstream"},
                ),
            )
        finally:
            client.close()

        body = json.loads(route.calls.last.request.content)
        assert body["campaignId"] == 4242
        assert body["summary"] == "chosen upstream"
        assert not listed.called

    @respx.mock
    def test_one_campaign_lookup_serves_a_whole_batch(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        listed = _campaign_list_route(st_settings, [{"id": 77, "name": REFERRAL_CAMPAIGN_NAME}])
        respx.post(f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}/leads").mock(
            return_value=httpx.Response(200, json={"id": 1})
        )

        client = ServiceTitanClient(st_settings)
        campaign = ReferralCampaign(client)
        try:
            for n in range(3):
                perform_item(
                    client,
                    OutboxItem(
                        id=str(n),
                        idempotency_key=f"key-{n}",
                        kind="referral_lead",
                        payload={"name": f"Friend {n}"},
                    ),
                    campaign,
                )
        finally:
            client.close()

        assert listed.call_count == 1


class TestTechnicianRating:
    def test_raises_unsupported_kind(self, st_settings: Settings) -> None:
        client = ServiceTitanClient(st_settings)
        try:
            item = OutboxItem(id="2", idempotency_key="key-2", kind="technician_rating", payload={})
            with pytest.raises(UnsupportedOutboxKindError, match="technician_rating"):
                perform_item(client, item)
        finally:
            client.close()


class TestUnknownKind:
    def test_raises_unsupported_kind(self, st_settings: Settings) -> None:
        client = ServiceTitanClient(st_settings)
        try:
            item = OutboxItem(id="3", idempotency_key="key-3", kind="something_else", payload={})
            with pytest.raises(UnsupportedOutboxKindError, match="something_else"):
                perform_item(client, item)
        finally:
            client.close()

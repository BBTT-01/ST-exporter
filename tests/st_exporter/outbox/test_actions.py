"""Tests for outbox action dispatch — one function per Outbox kind."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_cli.config import Settings
from st_exporter.outbox.actions import UnsupportedOutboxKindError, perform_item
from st_exporter.outbox.client import OutboxItem
from tests.st_exporter.conftest import mock_auth_token


class TestReferralLead:
    @respx.mock
    def test_creates_a_crm_lead_and_returns_its_id(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
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
        assert json.loads(route.calls.last.request.content) == {"name": "Jane"}


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

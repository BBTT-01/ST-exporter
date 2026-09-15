"""Tests for client.py."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_cli.config import Environment, Settings
from st_cli.exceptions import APIError, NotFoundError, RateLimitError, STCLIError, TransportError


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        client_id="test-id",
        client_secret="test-secret",
        app_key="test-key",
        tenant_id=12345,
        environment=Environment.PRODUCTION,
    )


@pytest.fixture()
def client(settings):
    """Client with a mocked TokenManager so no real auth calls happen."""
    with patch("st_cli.client.TokenManager") as MockTM:
        MockTM.return_value.get_token.return_value = "fake-token"
        MockTM.return_value.force_refresh.return_value = "refreshed-token"
        c = ServiceTitanClient(settings)
        yield c
        c.close()


class TestServiceTitanClient:
    @respx.mock
    def test_get_success(self, client):
        url = "/crm/v2/tenant/12345/customers"
        respx.get(url).mock(return_value=httpx.Response(200, json={"data": []}))
        result = client.get("crm", "customers")
        assert result == {"data": []}

    @respx.mock
    def test_post_success(self, client):
        url = "/crm/v2/tenant/12345/customers"
        respx.post(url).mock(return_value=httpx.Response(200, json={"id": 1}))
        result = client.post("crm", "customers", json_body={"name": "Test"})
        assert result == {"id": 1}

    @respx.mock
    def test_patch_success(self, client):
        url = "/crm/v2/tenant/12345/customers/1"
        respx.patch(url).mock(return_value=httpx.Response(200, json={"id": 1}))
        result = client.patch("crm", "customers/1", json_body={"name": "New"})
        assert result == {"id": 1}

    @respx.mock
    def test_put_success(self, client):
        url = "/pricebook/v2/tenant/12345/services/1"
        respx.put(url).mock(return_value=httpx.Response(200, json={"id": 1}))
        result = client.put("pricebook", "services/1", json_body={"name": "New"})
        assert result == {"id": 1}

    @respx.mock
    def test_delete_success(self, client):
        url = "/pricebook/v2/tenant/12345/services/1"
        respx.delete(url).mock(return_value=httpx.Response(204))
        result = client.delete("pricebook", "services/1")
        assert result is None

    @respx.mock
    def test_delete_passes_params(self, client):
        url = "/crm/v2/tenant/12345/customers/1/tags"
        route = respx.delete(url).mock(return_value=httpx.Response(200, json={"ok": True}))
        client.delete("crm", "customers/1/tags", params={"tagId": 7})
        assert "tagId=7" in str(route.calls[0].request.url)

    @respx.mock
    def test_404_raises_not_found(self, client):
        url = "/crm/v2/tenant/12345/customers/999"
        respx.get(url).mock(return_value=httpx.Response(404, text="not found"))
        with pytest.raises(NotFoundError):
            client.get("crm", "customers/999")

    @respx.mock
    def test_500_raises_api_error(self, client):
        url = "/crm/v2/tenant/12345/customers"
        respx.get(url).mock(return_value=httpx.Response(500, text="server error"))
        with pytest.raises(APIError) as exc_info:
            client.get("crm", "customers")
        assert exc_info.value.status_code == 500

    @respx.mock
    def test_401_triggers_token_refresh(self, client):
        url = "/crm/v2/tenant/12345/customers"
        respx.get(url).mock(
            side_effect=[
                httpx.Response(401, text="expired"),
                httpx.Response(200, json={"data": []}),
            ]
        )
        result = client.get("crm", "customers")
        assert result == {"data": []}

    @respx.mock
    def test_429_retries_with_backoff(self, client):
        url = "/crm/v2/tenant/12345/customers"
        respx.get(url).mock(
            side_effect=[
                httpx.Response(429, text="rate limited"),
                httpx.Response(200, json={"data": []}),
            ]
        )
        with patch("st_cli.client.time.sleep"):  # skip actual sleep
            result = client.get("crm", "customers")
        assert result == {"data": []}

    @respx.mock
    def test_429_exhausts_retries(self, client):
        url = "/crm/v2/tenant/12345/customers"
        respx.get(url).mock(return_value=httpx.Response(429, text="rate limited"))
        with patch("st_cli.client.time.sleep"):
            with pytest.raises(RateLimitError):
                client.get("crm", "customers")

    @respx.mock
    def test_204_returns_none(self, client):
        url = "/jpm/v2/tenant/12345/jobs/1/cancel"
        respx.post(url).mock(return_value=httpx.Response(204))
        result = client.post("jpm", "jobs/1/cancel")
        assert result is None

    @respx.mock
    def test_headers_include_auth_and_app_key(self, client):
        url = "/crm/v2/tenant/12345/customers"
        route = respx.get(url).mock(return_value=httpx.Response(200, json={}))
        client.get("crm", "customers")
        request = route.calls[0].request
        assert "Bearer" in request.headers["authorization"]
        assert request.headers["st-app-key"] == "test-key"

    @respx.mock
    def test_params_are_passed(self, client):
        url = "/crm/v2/tenant/12345/customers"
        route = respx.get(url).mock(return_value=httpx.Response(200, json={}))
        client.get("crm", "customers", params={"name": "Acme"})
        request = route.calls[0].request
        assert "name=Acme" in str(request.url)


class TestTransportFailures:
    """A request that never reaches an HTTP status still leaves as an STCLIError.

    This is what makes ``except STCLIError`` a complete guard. It is not a
    theoretical tidiness: the exporter's financial feed guards each of its four
    money tabs with exactly that clause, and a bare ``httpx.ReadTimeout`` on the
    report POST used to walk straight past it and discard three tabs that had
    already been fetched successfully.
    """

    @respx.mock
    def test_a_read_timeout_is_raised_as_a_transport_error(self, client):
        respx.get("/crm/v2/tenant/12345/customers").mock(side_effect=httpx.ReadTimeout("timed out"))
        with patch("st_cli.client.time.sleep"):
            with pytest.raises(TransportError) as excinfo:
                client.get("crm", "customers")
        assert isinstance(excinfo.value, STCLIError)
        # The original cause is chained, never swallowed.
        assert isinstance(excinfo.value.__cause__, httpx.ReadTimeout)
        assert "customers" in str(excinfo.value)

    @respx.mock
    def test_a_connect_error_is_retried_before_it_is_raised(self, client):
        route = respx.get("/crm/v2/tenant/12345/customers").mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        with patch("st_cli.client.time.sleep"):
            with pytest.raises(TransportError):
                client.get("crm", "customers")
        # A timeout is far more often a blip than a verdict, so it gets the same
        # retry budget a 429 does: the first attempt plus _MAX_RETRIES.
        assert route.call_count == 4

    @respx.mock
    def test_a_transient_transport_failure_recovers_without_raising(self, client):
        respx.get("/crm/v2/tenant/12345/customers").mock(
            side_effect=[
                httpx.ConnectError("no route to host"),
                httpx.Response(200, json={"data": [{"id": 1}]}),
            ]
        )
        with patch("st_cli.client.time.sleep"):
            assert client.get("crm", "customers") == {"data": [{"id": 1}]}

    @respx.mock
    def test_get_bytes_is_guarded_too(self, client):
        respx.get("/pricebook/v2/tenant/12345/images").mock(
            side_effect=httpx.ConnectTimeout("timed out")
        )
        with patch("st_cli.client.time.sleep"):
            with pytest.raises(TransportError):
                client.get_bytes("pricebook", "images", params={"path": "x.jpg"})


class TestWritesAreNeverResent:
    """A retry must never be able to create a SECOND ServiceTitan record.

    A `ReadTimeout` or a `RemoteProtocolError` after a POST means the request
    very likely reached the server and only the answer was lost. Re-issuing it
    books a second job, a second lead, a second booking — and the outbox writes
    its idempotency ledger only after `perform` returns, so nothing downstream
    can de-duplicate it. Failing the run is the cheap outcome; a duplicated
    record in a real contractor's ServiceTitan is not.
    """

    @respx.mock
    def test_a_post_is_not_resent_after_a_read_timeout(self, client):
        route = respx.post("/jpm/v2/tenant/12345/jobs").mock(
            side_effect=[
                httpx.ReadTimeout("response never arrived"),
                httpx.Response(200, json={"id": 2}),
            ]
        )
        with patch("st_cli.client.time.sleep"):
            with pytest.raises(TransportError):
                client.post("jpm", "jobs", json_body={"summary": "x"})
        assert route.call_count == 1

    @respx.mock
    def test_a_post_is_not_resent_after_a_protocol_error(self, client):
        route = respx.post("/jpm/v2/tenant/12345/jobs").mock(
            side_effect=[
                httpx.RemoteProtocolError("server disconnected mid-response"),
                httpx.Response(200, json={"id": 2}),
            ]
        )
        with patch("st_cli.client.time.sleep"):
            with pytest.raises(TransportError):
                client.post("jpm", "jobs", json_body={"summary": "x"})
        assert route.call_count == 1

    @respx.mock
    def test_the_message_says_the_write_was_deliberately_not_retried(self, client):
        respx.post("/jpm/v2/tenant/12345/jobs").mock(side_effect=httpx.ReadTimeout("gone"))
        with pytest.raises(TransportError) as excinfo:
            client.post("jpm", "jobs", json_body={})
        assert "not retried" in str(excinfo.value)

    @respx.mock
    def test_a_put_is_not_resent_either(self, client):
        route = respx.put("/jpm/v2/tenant/12345/jobs/7").mock(
            side_effect=[httpx.ReadTimeout("gone"), httpx.Response(200, json={})]
        )
        with patch("st_cli.client.time.sleep"):
            with pytest.raises(TransportError):
                client.put("jpm", "jobs/7", json_body={})
        assert route.call_count == 1

    @respx.mock
    def test_a_connect_failure_proves_the_write_never_left_so_it_is_retried(self, client):
        # Nothing was sent, so there is nothing to duplicate — and a DNS blip
        # must not fail a booking.
        route = respx.post("/jpm/v2/tenant/12345/jobs").mock(
            side_effect=[
                httpx.ConnectError("no route to host"),
                httpx.Response(200, json={"id": 2}),
            ]
        )
        with patch("st_cli.client.time.sleep"):
            assert client.post("jpm", "jobs", json_body={"summary": "x"}) == {"id": 2}
        assert route.call_count == 2

    @respx.mock
    def test_a_read_is_still_retried_freely(self, client):
        # A GET has no effect to duplicate; this is the behaviour that keeps a
        # network blip from discarding three already-fetched tabs.
        route = respx.get("/crm/v2/tenant/12345/customers").mock(
            side_effect=[httpx.ReadTimeout("timed out"), httpx.Response(200, json={"data": []})]
        )
        with patch("st_cli.client.time.sleep"):
            assert client.get("crm", "customers") == {"data": []}
        assert route.call_count == 2

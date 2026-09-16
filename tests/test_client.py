"""Tests for client.py."""

from __future__ import annotations

import logging
import time
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
    def test_a_post_declared_idempotent_is_resent_after_a_read_timeout(self, client):
        """`idempotent=True` is the one narrow exemption: a POST that is a READ.

        ServiceTitan's `POST reporting/.../data` runs a report — its parameters
        are in the body only because they do not fit a query string — so a lost
        answer may be re-asked for, exactly like a GET. Without this, a 90-day
        Job Costing Summary against a 30s client timeout fails every six-hourly
        run.
        """
        route = respx.post("/reporting/v2/tenant/12345/report-category/c/reports/r/data").mock(
            side_effect=[
                httpx.ReadTimeout("report generation > 30s"),
                httpx.Response(200, json={"data": []}),
            ]
        )
        with patch("st_cli.client.time.sleep"):
            answer = client.post(
                "reporting",
                "report-category/c/reports/r/data",
                json_body={"parameters": []},
                idempotent=True,
            )
        assert answer == {"data": []}
        assert route.call_count == 2

    @respx.mock
    def test_the_exemption_is_opt_in_so_a_plain_post_is_still_never_resent(self, client):
        """The flag must default off. A create that forgets it fails loudly; a
        create that is silently exempted duplicates a real booking."""
        route = respx.post("/crm/v2/tenant/12345/booking-provider/7/bookings").mock(
            side_effect=[
                httpx.ReadTimeout("response never arrived"),
                httpx.Response(200, json={"id": 2}),
            ]
        )
        with patch("st_cli.client.time.sleep"):
            with pytest.raises(TransportError):
                client.post("crm", "booking-provider/7/bookings", json_body={"name": "Jane"})
        assert route.call_count == 1

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


class TestConditionalFileFetch:
    """`get_file` is what lets the image pass ask "have these bytes changed?".

    Nobody has confirmed ServiceTitan honours `If-None-Match`, so the contract
    pinned here is the one that has to hold either way: the headers go out
    ALONGSIDE the auth headers, and a 304 comes back as a result rather than as
    an exception.
    """

    @respx.mock
    def test_conditional_headers_ride_with_the_auth_headers(self, client, settings):
        route = respx.get(
            f"{settings.api_base}/pricebook/v2/tenant/{settings.tenant_id}/images"
        ).mock(return_value=httpx.Response(200, content=b"bytes", headers={"etag": '"v1"'}))

        fetched = client.get_file(
            "pricebook", "images", params={"path": "x.jpg"}, headers={"If-None-Match": '"v1"'}
        )

        request = route.calls[0].request
        assert request.headers["if-none-match"] == '"v1"'
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["st-app-key"]
        assert (fetched.status_code, fetched.content, fetched.etag) == (200, b"bytes", '"v1"')
        assert fetched.has_validator is True
        assert fetched.not_modified is False

    @respx.mock
    def test_a_304_is_a_result_not_an_error(self, client, settings):
        respx.get(f"{settings.api_base}/pricebook/v2/tenant/{settings.tenant_id}/images").mock(
            return_value=httpx.Response(304, headers={"etag": '"v1"'})
        )

        fetched = client.get_file(
            "pricebook", "images", params={"path": "x.jpg"}, headers={"If-None-Match": '"v1"'}
        )

        assert fetched.not_modified is True
        assert fetched.content == b""

    @respx.mock
    def test_no_validator_in_the_response_is_reported_not_invented(self, client, settings):
        respx.get(f"{settings.api_base}/pricebook/v2/tenant/{settings.tenant_id}/images").mock(
            return_value=httpx.Response(200, content=b"bytes")
        )

        fetched = client.get_file("pricebook", "images", params={"path": "x.jpg"})

        assert (fetched.etag, fetched.last_modified) == (None, None)
        assert fetched.has_validator is False


class TestItWaitsAsLongAsTheServerAsks:
    """A 429 says how long to wait. Believing it is the difference between
    `reporting.jobCosts` being writable and being unreachable for ever.

    The exponential curve is 1s + 2s + 4s = SEVEN seconds of total patience.
    ServiceTitan's reporting endpoint allows roughly one run of the same report
    per minute per tenant and counts each PAGE as a run, so page 2 of any
    multi-page report is throttled by page 1 and asks for ~50 seconds. Seven
    seconds against a fifty-second ask fails 100% of the time — run 35158215902
    on `BBTT-01/tr-doorservpro`, twice, identically.
    """

    @respx.mock
    def test_it_reads_the_wait_out_of_servicetitans_problem_body(self, client, monkeypatch):
        """ServiceTitan puts the number in the BODY, not the header. Reading only
        the header is the same as reading nothing on the one endpoint that needs
        this."""
        slept = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        route = respx.get(url__regex=r".*/jpm/v2/tenant/.*").mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={
                        "status": 429,
                        "title": "Rate limit is exceeded. Try again in 50 seconds.",
                    },
                ),
                httpx.Response(200, json={"data": [], "hasMore": False}),
            ]
        )
        client.get("jpm", "jobs")
        assert slept == [50.0]
        assert route.call_count == 2

    @respx.mock
    def test_the_retry_after_header_is_preferred_when_present(self, client, monkeypatch):
        slept = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        respx.get(url__regex=r".*/jpm/v2/tenant/.*").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "12"}, text="Try again in 50 seconds"),
                httpx.Response(200, json={"data": [], "hasMore": False}),
            ]
        )
        client.get("jpm", "jobs")
        assert slept == [12.0]

    @respx.mock
    def test_a_429_with_no_stated_wait_still_uses_the_old_curve(self, client, monkeypatch):
        """Nothing regresses for an endpoint that says nothing."""
        slept = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        respx.get(url__regex=r".*/jpm/v2/tenant/.*").mock(
            side_effect=[
                httpx.Response(429, text="slow down"),
                httpx.Response(200, json={"data": [], "hasMore": False}),
            ]
        )
        client.get("jpm", "jobs")
        assert slept == [1.0]

    @respx.mock
    def test_an_absurd_ask_is_refused_rather_than_parking_the_run(self, client, monkeypatch):
        """A job killed by the runner loses everything it had already done; a
        caller that skips one tab is recoverable. So there is a ceiling."""
        slept = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        respx.get(url__regex=r".*/jpm/v2/tenant/.*").mock(
            return_value=httpx.Response(
                429, json={"title": "Rate limit is exceeded. Try again in 3600 seconds."}
            )
        )
        with pytest.raises(RateLimitError) as excinfo:
            client.get("jpm", "jobs")
        assert slept == []
        assert "3600" in str(excinfo.value)


class TestALongWaitSaysSoBeforeItStarts:
    """A 429 the client honours can park a request for a minute and a half, and
    until this existed it did so in complete silence.

    Silence is indistinguishable from a hang: run 35159471697 on
    `BBTT-01/tr-doorservpro` succeeded and wrote 1563 job-cost rows, but spent
    22:51:34 to 22:58:44 emitting nothing at all while it waited its way through
    a paginated report. The line has to come BEFORE the sleep — one printed
    afterwards tells whoever was deciding whether to cancel exactly nothing.
    """

    @pytest.fixture(autouse=True)
    def _capture_client_logs(self, caplog):
        """``st_exporter.logging_setup.configure_logging`` sets ``propagate =
        False`` on this logger too; caplog needs it back on."""
        log = logging.getLogger("st_cli")
        previous = log.propagate
        log.propagate = True
        yield
        log.propagate = previous

    @respx.mock
    def test_a_server_stated_wait_is_announced_with_its_length(
        self, client, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(time, "sleep", lambda s: None)
        respx.get(url__regex=r".*/jpm/v2/tenant/.*").mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={"title": "Rate limit is exceeded. Try again in 50 seconds."},
                ),
                httpx.Response(200, json={"data": [], "hasMore": False}),
            ]
        )
        with caplog.at_level(logging.INFO, logger="st_cli.client"):
            client.get("jpm", "jobs")

        assert "50s" in caplog.text
        assert "not a hang" in caplog.text.lower()

    @respx.mock
    def test_the_ordinary_one_second_backoff_stays_quiet(
        self, client, monkeypatch, caplog
    ) -> None:
        """The blind curve is the ordinary noise of a busy endpoint. The pricebook
        image pass alone can earn hundreds of those across its workers, and
        narrating every one would bury the run log the line exists to make
        readable."""
        monkeypatch.setattr(time, "sleep", lambda s: None)
        respx.get(url__regex=r".*/jpm/v2/tenant/.*").mock(
            side_effect=[
                httpx.Response(429, text="slow down"),
                httpx.Response(200, json={"data": [], "hasMore": False}),
            ]
        )
        with caplog.at_level(logging.INFO, logger="st_cli.client"):
            client.get("jpm", "jobs")

        assert caplog.text == ""

    @respx.mock
    def test_no_presigned_redirect_target_reaches_the_log(
        self, client, monkeypatch, caplog
    ) -> None:
        """The line names the ServiceTitan resource, never the request's current
        url: on an image fetch that url has been rebound to a blob address whose
        query string is a credential."""
        monkeypatch.setattr(time, "sleep", lambda s: None)
        respx.get(url__regex=r".*/pricebook/v2/tenant/.*").mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://blob.example.com/i.jpg?sig=SECRETSIG"}
            )
        )
        respx.get("https://blob.example.com/i.jpg").mock(
            side_effect=[
                httpx.Response(429, json={"title": "Rate limit is exceeded. Try again in 30 seconds."}),
                httpx.Response(200, content=b"\x89PNG\r\n\x1a\n", headers={"Content-Type": "image/png"}),
            ]
        )
        with caplog.at_level(logging.INFO, logger="st_cli.client"):
            client.get_file("pricebook", "images", params={"path": "x.jpg"})

        assert "SECRETSIG" not in caplog.text
        assert "30s" in caplog.text

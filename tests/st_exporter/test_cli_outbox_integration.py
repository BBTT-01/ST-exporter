"""Integration test for ``cli._drain_outboxes``'s real object wiring.

Every test in ``test_cli.py``'s ``TestOutboxDrain`` patches ``_drain_outboxes``
itself, so none of them exercise the seam this covers: real lane credentials
read from the environment, a real ``OutboxLedger`` over a Sheets store, real
per-product outbox clients, and a real ``ServiceTitanClient``, actually
constructed and wired to each other. That gap is why the empty-string-secret bug
(``configured`` returning True for ``""``, then ``httpx.UnsupportedProtocol``
from a client built on an empty base URL) reached a review instead of a test.

Only Google Sheets is faked (there is no local gspread to point at); every HTTP
side — ServiceTitan, TradeRated, TrueQuote — is respx-mocked, so the whole call
chain below the CLI is the production code.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
import respx

from st_exporter.cli import _drain_outboxes, main
from st_exporter.outbox.drain import DrainSummary
from st_exporter.run import ExportSummary
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token

OUTBOX_BASE_URL = "https://outbox.traderated.test"
MACHINE_TOKEN = "machine-token-xyz"
TQ_BASE_URL = "https://truequote.test/api/outbox"
TQ_TOKEN = "tqm_booking"

_DRAIN_ARGV = ["st-export", "--feeds", "jobs,technicians,outbox"]


@pytest.fixture()
def traderated_env(monkeypatch):
    """Real, non-empty TRADERATED_* env vars (overriding conftest's autouse
    delenv), so the lane is loaded exactly as it is in Actions."""
    monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", MACHINE_TOKEN)
    monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", OUTBOX_BASE_URL)


@pytest.fixture()
def truequote_env(monkeypatch):
    monkeypatch.setenv("TRUEQUOTE_MACHINE_TOKEN", TQ_TOKEN)
    monkeypatch.setenv("TRUEQUOTE_OUTBOX_URL", TQ_BASE_URL)


@respx.mock
def test_drain_outbox_wires_real_clients_and_ledger_end_to_end(
    traderated_env, st_settings, exporter_settings
) -> None:
    raw_cache_store = InMemorySheetsStore()

    mock_auth_token(st_settings.auth_url)
    # A referral lead cannot be created without a campaignId, so the real wiring now
    # resolves the referral campaign before posting the lead.
    campaign_route = respx.get(
        f"{st_settings.api_base}/marketing/v2/tenant/{st_settings.tenant_id}/campaigns"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": 31, "name": "TradeRated Referrals"}], "hasMore": False},
        )
    )
    lead_route = respx.post(
        f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}/leads"
    ).mock(return_value=httpx.Response(200, json={"id": 4242}))
    rating_route = respx.post(
        f"{st_settings.api_base}/customer-interactions/v2/tenant/"
        f"{st_settings.tenant_id}/technician-ratings"
    ).mock(return_value=httpx.Response(200, json={}))
    claim_route = respx.get(f"{OUTBOX_BASE_URL}/crm-outbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "count": 2,
                "items": [
                    {
                        "id": "item-1",
                        "idempotency_key": "key-1",
                        "kind": "referral_lead",
                        "payload": {"name": "Jane Doe"},
                        "attempts": 1,
                    },
                    {
                        "id": "item-2",
                        "idempotency_key": "key-2",
                        "kind": "technician_rating",
                        "payload": {
                            "review_id": "r-1",
                            "rating": 5,
                            "servicetitan_job_id": "27073478",
                            "servicetitan_technician_id": "14941693",
                        },
                        "attempts": 1,
                    },
                ],
            },
        )
    )
    report_route = respx.post(url__regex=rf"{OUTBOX_BASE_URL}/crm-outbox/.+/result").mock(
        return_value=httpx.Response(200, json={})
    )

    with (
        patch("st_exporter.cli.get_gspread_client") as mock_gspread,
        patch("st_exporter.cli.SheetsClient") as mock_sheets_client,
    ):
        mock_sheets_client.open.return_value = raw_cache_store
        outcomes = _drain_outboxes(st_settings, exporter_settings)

    # BOTH kinds now succeed. `technician_rating` used to be reported failed
    # against a permission every contractor had already granted.
    assert [outcome.product for outcome in outcomes] == ["traderated"]
    assert outcomes[0].summary == DrainSummary(claimed=2, succeeded=2, failed=0, replayed=0)

    mock_gspread.assert_called_once_with(exporter_settings.service_account_json)
    mock_sheets_client.open.assert_called_once_with(
        mock_gspread.return_value, exporter_settings.raw_cache_sheet_id
    )

    assert claim_route.called
    assert claim_route.calls.last.request.headers["Authorization"] == f"Bearer {MACHINE_TOKEN}"
    assert campaign_route.called, "the referral campaign must be resolved before the lead"
    lead_body = json.loads(lead_route.calls.last.request.content)
    # Supplied by the exporter, not TradeRated: ServiceTitan rejects a lead without any of
    # these three, and TradeRated's payload carries none of them.
    assert lead_body["name"] == "Jane Doe"
    assert lead_body["campaignId"] == 31
    assert lead_body["summary"] == "Referral from TradeRated for Jane Doe"
    assert lead_body["followUpDate"]

    rating_body = json.loads(rating_route.calls.last.request.content)
    assert rating_body == {"technicianId": 14941693, "jobId": 27073478, "rating": 10}

    reports = {
        call.request.url.path: json.loads(call.request.content) for call in report_route.calls
    }
    assert reports["/crm-outbox/item-1/result"] == {"status": "succeeded", "st_id": "4242"}
    assert reports["/crm-outbox/item-2/result"] == {
        "status": "succeeded",
        "st_id": "14941693:27073478",
    }

    # The performed items are durable in the ledger tab, so a redelivery next run
    # replays instead of creating a second lead.
    ledger_grid = raw_cache_store.tabs["_outbox_ledger"]
    assert ledger_grid[0] == ["idempotency_key", "kind", "st_id", "performed_at", "product"]
    assert {(row[0], row[4]) for row in ledger_grid[1:]} == {
        ("key-1", "traderated"),
        ("key-2", "traderated"),
    }


@respx.mock
def test_two_products_drain_in_one_run_against_their_own_endpoints(
    traderated_env, truequote_env, st_settings, exporter_settings
) -> None:
    """Ticket 12's headline case. Two apps, two base URLs, two path shapes, two
    result vocabularies — and one ledger, one run, one ServiceTitan client."""
    mock_auth_token(st_settings.auth_url)
    respx.get(f"{st_settings.api_base}/marketing/v2/tenant/{st_settings.tenant_id}/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": 31, "name": "TradeRated Referrals"}], "hasMore": False},
        )
    )
    respx.post(f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}/leads").mock(
        return_value=httpx.Response(200, json={"id": 4242})
    )
    booking_route = respx.post(
        f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}/booking-provider/77/bookings"
    ).mock(return_value=httpx.Response(200, json={"id": 90210}))

    respx.get(f"{OUTBOX_BASE_URL}/crm-outbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "tr-1",
                        "idempotency_key": "key-1",
                        "kind": "referral_lead",
                        "payload": {"name": "Jane Doe"},
                    }
                ]
            },
        )
    )
    tr_report = respx.post(url__regex=rf"{OUTBOX_BASE_URL}/crm-outbox/.+/result").mock(
        return_value=httpx.Response(200, json={})
    )
    tq_claim = respx.post(f"{TQ_BASE_URL}/booking/claim").mock(
        return_value=httpx.Response(
            200,
            json={
                "lease_seconds": 300,
                "items": [
                    {
                        "item_id": "8b1b0f9e-0000-4000-8000-000000000000",
                        "idempotency_key": "servicetitan:booking:sess-1",
                        "tenant_id": "999",
                        "booking_provider_id": "77",
                        "booking": {"sessionId": "sess-1", "name": "Bob", "summary": "Two doors"},
                    }
                ],
            },
        )
    )
    tq_report = respx.post(f"{TQ_BASE_URL}/booking/result").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    raw_cache_store = InMemorySheetsStore()
    with (
        patch("st_exporter.cli.get_gspread_client"),
        patch("st_exporter.cli.SheetsClient") as mock_sheets_client,
    ):
        mock_sheets_client.open.return_value = raw_cache_store
        outcomes = _drain_outboxes(st_settings, exporter_settings)

    assert [outcome.product for outcome in outcomes] == ["traderated", "truequote"]
    assert all(outcome.summary == DrainSummary(1, 1, 0, 0) for outcome in outcomes)

    # Each lane presented its OWN token to its OWN host.
    assert tq_claim.calls.last.request.headers["Authorization"] == f"Bearer {TQ_TOKEN}"
    assert booking_route.called

    # Each lane reported in its OWN vocabulary: `st_id` for TradeRated,
    # `booking_id` for TrueQuote, which does not read `st_id` at all.
    assert json.loads(tr_report.calls.last.request.content)["st_id"] == "4242"
    tq_body = json.loads(tq_report.calls.last.request.content)
    assert tq_body["booking_id"] == "90210"
    assert "st_id" not in tq_body

    # One ledger tab, two products, no key space shared.
    products = {row[4] for row in raw_cache_store.tabs["_outbox_ledger"][1:]}
    assert products == {"traderated", "truequote"}


@respx.mock
def test_one_dead_lane_does_not_stop_the_other(
    traderated_env, truequote_env, st_settings, exporter_settings
) -> None:
    """The per-lane isolation requirement, end to end through the real clients."""
    mock_auth_token(st_settings.auth_url)
    booking_route = respx.post(
        f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}/booking-provider/77/bookings"
    ).mock(return_value=httpx.Response(200, json={"id": 90210}))

    dead_claim = respx.get(f"{OUTBOX_BASE_URL}/crm-outbox").mock(
        side_effect=httpx.ConnectError("refused")
    )
    respx.post(f"{TQ_BASE_URL}/booking/claim").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "item_id": "8b1b0f9e-0000-4000-8000-000000000000",
                        "idempotency_key": "servicetitan:booking:sess-1",
                        "booking_provider_id": "77",
                        "booking": {"sessionId": "sess-1", "summary": "Two doors"},
                    }
                ]
            },
        )
    )
    respx.post(f"{TQ_BASE_URL}/booking/result").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    with (
        patch("st_exporter.cli.get_gspread_client"),
        patch("st_exporter.cli.SheetsClient") as mock_sheets_client,
    ):
        mock_sheets_client.open.return_value = InMemorySheetsStore()
        outcomes = _drain_outboxes(st_settings, exporter_settings)

    assert dead_claim.called
    assert outcomes[0].product == "traderated" and outcomes[0].summary is None
    assert outcomes[1].summary == DrainSummary(1, 1, 0, 0)
    assert booking_route.called, "TrueQuote's booking must still reach ServiceTitan"


@respx.mock
def test_a_lane_returning_garbage_does_not_stop_the_other(
    traderated_env, truequote_env, st_settings, exporter_settings
) -> None:
    """Not merely "down": a 200 whose body is not the JSON this exporter reads."""
    mock_auth_token(st_settings.auth_url)
    respx.post(
        f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}/booking-provider/77/bookings"
    ).mock(return_value=httpx.Response(200, json={"id": 90210}))
    respx.get(f"{OUTBOX_BASE_URL}/crm-outbox").mock(
        return_value=httpx.Response(200, text="<html>502 Bad Gateway</html>")
    )
    respx.post(f"{TQ_BASE_URL}/booking/claim").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "item_id": "8b1b0f9e-0000-4000-8000-000000000000",
                        "idempotency_key": "servicetitan:booking:sess-1",
                        "booking_provider_id": "77",
                        "booking": {"sessionId": "sess-1", "summary": "Two doors"},
                    }
                ]
            },
        )
    )
    respx.post(f"{TQ_BASE_URL}/booking/result").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    with (
        patch("st_exporter.cli.get_gspread_client"),
        patch("st_exporter.cli.SheetsClient") as mock_sheets_client,
    ):
        mock_sheets_client.open.return_value = InMemorySheetsStore()
        outcomes = _drain_outboxes(st_settings, exporter_settings)

    assert outcomes[0].error is not None
    assert outcomes[1].summary == DrainSummary(1, 1, 0, 0)


@respx.mock
def test_empty_string_secrets_never_reach_the_outbox_client(
    st_settings, exporter_settings, monkeypatch, capsys
) -> None:
    """The regression guard for the real Actions failure mode: GitHub maps an
    ``env:`` entry for an unset secret to ``""``, and the drain must then skip
    outright — not construct a client on an empty base URL. respx is active with
    no routes registered, so any HTTP attempt would also fail loudly.
    """
    monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "")
    monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "")
    monkeypatch.setenv("TRUEQUOTE_MACHINE_TOKEN", "")
    monkeypatch.setenv("TRUEQUOTE_OUTBOX_URL", "")
    monkeypatch.setattr("sys.argv", _DRAIN_ARGV)

    summary = ExportSummary(
        jobs_row_count=1, technicians_row_count=1, skipped_no_job=0, dry_run=False
    )
    with (
        # Real settings objects, not sentinel strings: under the pre-fix
        # ``is not None`` logic the drain is entered, and it must get far enough
        # to actually call get_gspread_client for this assertion to have teeth.
        patch("st_exporter.cli.load_settings", return_value=st_settings),
        patch("st_exporter.cli.ExporterSettings", return_value=exporter_settings),
        patch("st_exporter.cli.run_export", return_value=summary),
        patch("st_exporter.cli.get_gspread_client") as mock_gspread,
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 0
    mock_gspread.assert_not_called()
    out = capsys.readouterr().out
    assert "jobs=1 technicians=1" in out
    assert "_claimed" not in out


@respx.mock
def test_unreachable_outbox_does_not_fail_the_run(
    traderated_env, st_settings, exporter_settings, capsys, monkeypatch
) -> None:
    """A real (non-empty) outbox URL that refuses the connection exercises the
    full wiring *and* the swallow-and-warn path: exit 0, export summary intact,
    no traceback."""
    monkeypatch.setattr("sys.argv", _DRAIN_ARGV)
    claim_route = respx.get(f"{OUTBOX_BASE_URL}/crm-outbox").mock(
        side_effect=httpx.ConnectError("refused")
    )

    summary = ExportSummary(
        jobs_row_count=2, technicians_row_count=0, skipped_no_job=0, dry_run=False
    )
    with (
        patch("st_exporter.cli.load_settings", return_value=st_settings),
        patch("st_exporter.cli.ExporterSettings", return_value=exporter_settings),
        patch("st_exporter.cli.run_export", return_value=summary),
        patch("st_exporter.cli.get_gspread_client"),
        patch("st_exporter.cli.SheetsClient") as mock_sheets_client,
        pytest.raises(SystemExit) as exc_info,
    ):
        mock_sheets_client.open.return_value = InMemorySheetsStore()
        main()

    assert exc_info.value.code == 0
    # Proves the failure came from the real claim call, not from something
    # blowing up before the outbox client was ever built.
    assert claim_route.called
    captured = capsys.readouterr()
    assert "jobs=2 technicians=0" in captured.out
    assert "traderated_lane_error=1" in captured.out
    assert "Traceback" not in captured.err

"""Integration test for ``cli._drain_outbox``'s real object wiring.

Every test in ``test_cli.py``'s ``TestOutboxDrain`` patches ``_drain_outbox``
itself, so none of them exercise the seam this covers: a real
``TradeRatedSettings`` read from the environment, a real ``OutboxLedger`` over a
Sheets store, a real ``TradeRatedOutboxClient``, and a real
``ServiceTitanClient``, actually constructed and wired to each other. That gap
is why the empty-string-secret bug (``configured`` returning True for ``""``,
then ``httpx.UnsupportedProtocol`` from a client built on an empty base URL)
reached a review instead of a test.

Only Google Sheets is faked (there is no local gspread to point at); both HTTP
sides — ServiceTitan and TradeRated — are respx-mocked, so the whole call chain
below the CLI is the production code.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
import respx

from st_exporter.cli import _drain_outbox, main
from st_exporter.outbox.drain import DrainSummary
from st_exporter.run import ExportSummary
from st_exporter.sheets import InMemorySheetsStore
from st_exporter.traderated_settings import TradeRatedSettings
from tests.st_exporter.conftest import mock_auth_token

OUTBOX_BASE_URL = "https://outbox.traderated.test"
MACHINE_TOKEN = "machine-token-xyz"


@pytest.fixture()
def traderated_env(monkeypatch):
    """Real, non-empty TRADERATED_* env vars (overriding conftest's autouse
    delenv), so ``TradeRatedSettings()`` is loaded exactly as it is in Actions."""
    monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", MACHINE_TOKEN)
    monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", OUTBOX_BASE_URL)


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
    claim_route = respx.get(f"{OUTBOX_BASE_URL}/crm-outbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "item-1",
                        "idempotency_key": "key-1",
                        "kind": "referral_lead",
                        "payload": {"name": "Jane Doe"},
                    },
                    {
                        "id": "item-2",
                        "idempotency_key": "key-2",
                        "kind": "technician_rating",
                        "payload": {},
                    },
                ]
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
        summary = _drain_outbox(st_settings, exporter_settings, TradeRatedSettings())

    # referral_lead performed; technician_rating has no ST write yet, so it is
    # reported failed rather than silently dropped.
    assert summary == DrainSummary(claimed=2, succeeded=1, failed=1, replayed=0)

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

    reports = {
        call.request.url.path: json.loads(call.request.content) for call in report_route.calls
    }
    assert reports["/crm-outbox/item-1/result"] == {"status": "succeeded", "st_id": "4242"}
    assert reports["/crm-outbox/item-2/result"]["status"] == "failed"

    # The performed item is durable in the ledger tab, so a redelivery next run
    # replays instead of creating a second lead.
    ledger_grid = raw_cache_store.tabs["_outbox_ledger"]
    assert ledger_grid[0] == ["idempotency_key", "kind", "st_id", "performed_at"]
    assert [row[:3] for row in ledger_grid[1:]] == [["key-1", "referral_lead", "4242"]]


@respx.mock
def test_empty_string_secrets_never_reach_the_outbox_client(
    st_settings, exporter_settings, monkeypatch, capsys
) -> None:
    """The regression guard for the real Actions failure mode: GitHub maps an
    ``env:`` entry for an unset secret to ``""``, and ``run_once`` must then skip
    the drain outright — not construct a client on an empty base URL. respx is
    active with no routes registered, so any HTTP attempt would also fail loudly.
    """
    monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "")
    monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "")
    monkeypatch.setattr("sys.argv", ["st-export"])

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
        patch("st_exporter.cli.TradeRatedOutboxClient") as mock_outbox_client,
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 0
    mock_gspread.assert_not_called()
    mock_outbox_client.assert_not_called()
    out = capsys.readouterr().out
    assert "jobs=1 technicians=1" in out
    assert "outbox_claimed" not in out


@respx.mock
def test_unreachable_outbox_does_not_fail_the_run(
    traderated_env, st_settings, exporter_settings, capsys, monkeypatch
) -> None:
    """A real (non-empty) outbox URL that refuses the connection exercises the
    full wiring *and* cli.py's swallow-and-warn path: exit 0, export summary
    intact, no traceback."""
    monkeypatch.setattr("sys.argv", ["st-export"])
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
    assert "outbox_claimed" not in captured.out
    assert "Traceback" not in captured.err

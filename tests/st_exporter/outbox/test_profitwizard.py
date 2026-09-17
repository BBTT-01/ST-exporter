"""Tests for Profit Wizard's lane — its own paths, envelope and result handling.

Their contract is live and verified end-to-end on staging: `POST {base}/claim`,
`POST {base}/result`, Machine Token scope `servicetitan_outbox`, company resolved
from the token record only, skip-locked claims, replay of a terminal result a
no-op, and a result contract that widens only.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_cli.config import Settings
from st_exporter.outbox.actions import UnsupportedOutboxKindError
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.profitwizard import (
    _UNIMPLEMENTED_KINDS,
    ProfitWizardOutboxClient,
    perform_profitwizard_item,
)
from tests.st_exporter.conftest import mock_auth_token

BASE = "https://profitwizard.test/api/outbox"
TOKEN = "pwm_servicetitan_outbox"


def _client() -> ProfitWizardOutboxClient:
    return ProfitWizardOutboxClient(BASE, TOKEN)


def _item(**overrides) -> OutboxItem:
    defaults = dict(id="item-1", idempotency_key="key-1", kind="update_job", payload={})
    defaults.update(overrides)
    return OutboxItem(**defaults)


class TestClaim:
    @respx.mock
    def test_claims_at_their_own_path_with_their_own_token(self) -> None:
        route = respx.post(f"{BASE}/claim").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        client = _client()
        try:
            assert client.claim(limit=5) == []
        finally:
            client.close()

        assert json.loads(route.calls.last.request.content) == {"limit": 5}
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"

    @respx.mock
    def test_reads_an_item_into_the_shape_the_drain_loop_holds(self) -> None:
        respx.post(f"{BASE}/claim").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "item_id": "pw-1",
                            "idempotency_key": "update_job:99",
                            "kind": "update_job",
                            "payload": {"jobId": 99},
                        }
                    ]
                },
            )
        )
        client = _client()
        try:
            items = client.claim()
        finally:
            client.close()

        assert items[0].id == "pw-1"
        assert items[0].idempotency_key == "update_job:99"
        assert items[0].kind == "update_job"
        assert items[0].payload == {"jobId": 99}

    @respx.mock
    def test_an_all_null_composite_row_is_not_an_item(self) -> None:
        """THE bug Profit Wizard hit from the other side, guarded from this one.

        Their claim RPC returns a composite row type, and PostgREST serialises
        "no match" as an object with every field null rather than as null. That
        object is TRUTHY, so only an identifying FIELD can tell a real row from
        an empty one — `if row:` would hand the drain loop a ghost item, perform
        nothing against ServiceTitan, and report a result for a row that does
        not exist.
        """
        respx.post(f"{BASE}/claim").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"item_id": None, "idempotency_key": None, "kind": None, "payload": None},
                        {"item_id": "pw-2", "idempotency_key": "k", "kind": "update_job"},
                    ]
                },
            )
        )
        client = _client()
        try:
            items = client.claim()
        finally:
            client.close()

        assert [item.id for item in items] == ["pw-2"]

    @respx.mock
    def test_an_envelope_keyed_jobs_is_read_too(self) -> None:
        """Profit Wizard's claim endpoint used to answer under `jobs`; now fixed
        to also return `items`, but the exporter must tolerate either — their
        contract widens only, so a key this reader once missed must not be read
        as an empty queue."""
        respx.post(f"{BASE}/claim").mock(
            return_value=httpx.Response(
                200,
                json={"jobs": [{"id": "pw-4", "idempotency_key": "k4", "kind": "update_job"}]},
            )
        )
        client = _client()
        try:
            assert [item.id for item in client.claim()] == ["pw-4"]
        finally:
            client.close()

    @respx.mock
    def test_a_bare_array_body_is_read_too(self) -> None:
        """Their contract widens; so does this reader. An envelope that is not
        `{"items": [...]}` must not be read as an empty queue."""
        respx.post(f"{BASE}/claim").mock(
            return_value=httpx.Response(
                200, json=[{"id": "pw-3", "idempotency_key": "k3", "kind": "push_prices"}]
            )
        )
        client = _client()
        try:
            assert [item.id for item in client.claim()] == ["pw-3"]
        finally:
            client.close()


class TestReport:
    @respx.mock
    def test_a_success_names_the_item_and_the_servicetitan_id(self) -> None:
        route = respx.post(f"{BASE}/result").mock(
            return_value=httpx.Response(200, json={"matched": True})
        )
        client = _client()
        try:
            client.report_success(_item(), "st-500")
        finally:
            client.close()

        body = json.loads(route.calls.last.request.content)
        assert body["item_id"] == "item-1"
        assert body["idempotency_key"] == "key-1"
        assert body["status"] == "succeeded"
        # Both spellings: their contract accepts several per field and widens
        # only, so sending both costs nothing and survives either being dropped.
        assert body["st_id"] == "st-500"
        assert body["external_id"] == "st-500"

    @respx.mock
    def test_a_failure_carries_the_error_text(self) -> None:
        route = respx.post(f"{BASE}/result").mock(
            return_value=httpx.Response(200, json={"matched": True})
        )
        client = _client()
        try:
            client.report_failure(_item(), "ServiceTitan 404")
        finally:
            client.close()
        body = json.loads(route.calls.last.request.content)
        assert body["status"] == "failed"
        assert body["error"] == "ServiceTitan 404"

    @respx.mock
    def test_matched_false_is_warned_about_and_is_not_an_exception(self, caplog) -> None:
        """200 + `matched: false` is neither success nor a hard failure. Raising
        would abandon every remaining item in the batch over a row that is
        already written in ServiceTitan and already durable in our ledger."""
        respx.post(f"{BASE}/result").mock(return_value=httpx.Response(200, json={"matched": False}))
        client = _client()
        try:
            with caplog.at_level(logging.WARNING):
                client.report_success(_item(), "st-500")
        finally:
            client.close()

        assert "matched no queue row" in caplog.text
        assert "key-1" in caplog.text

    @respx.mock
    def test_matched_true_is_silent(self, caplog) -> None:
        respx.post(f"{BASE}/result").mock(
            return_value=httpx.Response(200, json={"matched": True, "status": "succeeded"})
        )
        client = _client()
        try:
            with caplog.at_level(logging.WARNING):
                client.report_success(_item(), "st-500")
        finally:
            client.close()
        assert "matched no queue row" not in caplog.text

    @respx.mock
    def test_an_all_null_nested_row_counts_as_unmatched(self, caplog) -> None:
        """The composite-row trap again, on the response side: `if body["item"]`
        is True for an object of nulls, so the id field is what decides."""
        respx.post(f"{BASE}/result").mock(
            return_value=httpx.Response(
                200, json={"item": {"item_id": None, "idempotency_key": None}}
            )
        )
        client = _client()
        try:
            with caplog.at_level(logging.WARNING):
                client.report_success(_item(), "st-500")
        finally:
            client.close()
        assert "matched no queue row" in caplog.text

    @respx.mock
    def test_a_body_that_says_nothing_is_not_read_as_unmatched(self, caplog) -> None:
        """An endpoint answering `{}` or a non-JSON 200 must not be turned into a
        warning on every single item — silence is not evidence of no match."""
        respx.post(f"{BASE}/result").mock(return_value=httpx.Response(200, json={}))
        client = _client()
        try:
            with caplog.at_level(logging.WARNING):
                client.report_success(_item(), "st-500")
        finally:
            client.close()
        assert "matched no queue row" not in caplog.text

    @respx.mock
    def test_a_real_http_error_still_raises(self) -> None:
        """Widening the accepted vocabulary must not swallow a 500."""
        respx.post(f"{BASE}/result").mock(return_value=httpx.Response(500, text="boom"))
        client = _client()
        try:
            with pytest.raises(httpx.HTTPStatusError):
                client.report_success(_item(), "st-500")
        finally:
            client.close()


class TestPerform:
    """Three of the four writes still belong to ticket 15, which is blocked by
    this ticket. The LANE is real; their ServiceTitan request bodies are not
    knowable here yet. `assign_technician` is implemented — see
    `TestAssignTechnician` below."""

    def test_each_unimplemented_kind_names_itself_and_its_ticket(
        self, st_settings: Settings
    ) -> None:
        client = ServiceTitanClient(st_settings)
        try:
            for kind in _UNIMPLEMENTED_KINDS:
                with pytest.raises(UnsupportedOutboxKindError) as exc:
                    perform_profitwizard_item(client, _item(kind=kind))
                assert kind in str(exc.value)
                assert "ticket 15" in str(exc.value)
        finally:
            client.close()

    def test_an_unrecognised_kind_reads_differently_from_an_unbuilt_one(
        self, st_settings: Settings
    ) -> None:
        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(UnsupportedOutboxKindError, match="unknown profitwizard"):
                perform_profitwizard_item(client, _item(kind="teleport_van"))
        finally:
            client.close()


class TestClaimIdentityGuardMatchesTrueQuote:
    """Both lanes refuse an item missing EITHER identity field.

    They drifted: Profit Wizard dropped a row only when both were blank, while
    TrueQuote kept blank-keyed rows outright. A blank `idempotency_key` alone is
    enough to collapse every such item onto one ledger key and report a write
    that never happened as delivered, so "either" is not enough.
    """

    @respx.mock
    def test_a_row_with_a_key_but_no_id_is_not_an_item(self) -> None:
        respx.post(f"{BASE}/claim").mock(
            return_value=httpx.Response(
                200,
                json={"items": [{"idempotency_key": "k", "kind": "update_job", "payload": {}}]},
            )
        )
        client = ProfitWizardOutboxClient(BASE, TOKEN)
        try:
            assert client.claim() == []
        finally:
            client.close()

    @respx.mock
    def test_a_row_with_an_id_but_no_key_is_not_an_item(self) -> None:
        respx.post(f"{BASE}/claim").mock(
            return_value=httpx.Response(
                200,
                json={"items": [{"item_id": "pw-9", "kind": "update_job", "payload": {}}]},
            )
        )
        client = ProfitWizardOutboxClient(BASE, TOKEN)
        try:
            assert client.claim() == []
        finally:
            client.close()


def _assign_item(**overrides) -> OutboxItem:
    payload = {
        "crmJobId": "9001",
        "jobScheduledStart": None,
        "technicianCrmIds": [],
        "authorizedRemovalCrmIds": [],
    }
    payload.update(overrides.pop("payload", {}))
    defaults = dict(
        id="item-assign",
        idempotency_key="assign_technician:9001",
        kind="assign_technician",
        payload=payload,
    )
    defaults.update(overrides)
    return OutboxItem(**defaults)


def _appointments_route(st_settings: Settings, records: list[dict]) -> respx.Route:
    return respx.get(
        f"{st_settings.api_base}/jpm/v2/tenant/{st_settings.tenant_id}/appointments"
    ).mock(return_value=httpx.Response(200, json={"data": records, "hasMore": False}))


def _assignments_route(st_settings: Settings, records: list[dict]) -> respx.Route:
    return respx.get(
        f"{st_settings.api_base}/dispatch/v2/tenant/{st_settings.tenant_id}/appointment-assignments"
    ).mock(return_value=httpx.Response(200, json={"data": records, "hasMore": False}))


def _assign_route(st_settings: Settings) -> respx.Route:
    return respx.post(
        f"{st_settings.api_base}/dispatch/v2/tenant/{st_settings.tenant_id}"
        "/appointment-assignments/assign-technicians"
    ).mock(return_value=httpx.Response(200, json={}))


def _unassign_route(st_settings: Settings) -> respx.Route:
    return respx.post(
        f"{st_settings.api_base}/dispatch/v2/tenant/{st_settings.tenant_id}"
        "/appointment-assignments/unassign-technicians"
    ).mock(return_value=httpx.Response(200, json={}))


def _assignment_row(appointment_id: int, technician_id: int, *, active: bool = True) -> dict:
    return {
        "id": technician_id * 100,
        "technicianId": technician_id,
        "appointmentId": appointment_id,
        "active": active,
        "status": "Active" if active else "Removed",
    }


class TestAssignTechnician:
    """`assign_technician` mirrors PW's own direct-CRM write
    (`setAppointmentTechnicianSet` in `lib/crm/servicetitan.ts`): resolve the
    job's appointment, diff the intended crew against who's currently active,
    and unassign ONLY ids the payload's `authorizedRemovalCrmIds` names."""

    @respx.mock
    def test_assign_only_when_nothing_needs_removing(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _appointments_route(
            st_settings, [{"id": 555, "start": "2026-01-01T10:00:00Z", "status": "Scheduled"}]
        )
        _assignments_route(st_settings, [])
        assign_route = _assign_route(st_settings)

        client = ServiceTitanClient(st_settings)
        try:
            st_id = perform_profitwizard_item(
                client,
                _assign_item(
                    payload={"technicianCrmIds": ["10", "20"], "authorizedRemovalCrmIds": []}
                ),
            )
        finally:
            client.close()

        assert st_id == "555"
        assert assign_route.called
        body = json.loads(assign_route.calls.last.request.content)
        assert body == {"jobAppointmentId": 555, "technicianIds": [10, 20]}

    @respx.mock
    def test_unassign_only_when_authorized_and_currently_assigned(
        self, st_settings: Settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _appointments_route(
            st_settings, [{"id": 555, "start": "2026-01-01T10:00:00Z", "status": "Scheduled"}]
        )
        _assignments_route(st_settings, [_assignment_row(555, 30)])
        unassign_route = _unassign_route(st_settings)

        client = ServiceTitanClient(st_settings)
        try:
            st_id = perform_profitwizard_item(
                client,
                _assign_item(payload={"technicianCrmIds": [], "authorizedRemovalCrmIds": ["30"]}),
            )
        finally:
            client.close()

        assert st_id == "555"
        assert unassign_route.called
        body = json.loads(unassign_route.calls.last.request.content)
        assert body == {"jobAppointmentId": 555, "technicianIds": [30]}

    @respx.mock
    def test_mixed_assign_and_unassign(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _appointments_route(
            st_settings, [{"id": 555, "start": "2026-01-01T10:00:00Z", "status": "Scheduled"}]
        )
        _assignments_route(st_settings, [_assignment_row(555, 30), _assignment_row(555, 40)])
        assign_route = _assign_route(st_settings)
        unassign_route = _unassign_route(st_settings)

        client = ServiceTitanClient(st_settings)
        try:
            perform_profitwizard_item(
                client,
                _assign_item(
                    payload={
                        "technicianCrmIds": ["10"],
                        "authorizedRemovalCrmIds": ["30"],
                    }
                ),
            )
        finally:
            client.close()

        assert json.loads(assign_route.calls.last.request.content) == {
            "jobAppointmentId": 555,
            "technicianIds": [10],
        }
        assert json.loads(unassign_route.calls.last.request.content) == {
            "jobAppointmentId": 555,
            "technicianIds": [30],
        }
        # Technician 40 is neither intended nor authorized for removal — never
        # named in either call.
        assert 40 not in json.loads(unassign_route.calls.last.request.content)["technicianIds"]

    @respx.mock
    def test_no_op_makes_no_servicetitan_write_call(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _appointments_route(
            st_settings, [{"id": 555, "start": "2026-01-01T10:00:00Z", "status": "Scheduled"}]
        )
        _assignments_route(st_settings, [_assignment_row(555, 10)])
        # No assign/unassign routes registered at all: if the exporter posts to
        # either, respx raises on the unmatched request and this test fails.

        client = ServiceTitanClient(st_settings)
        try:
            st_id = perform_profitwizard_item(
                client,
                _assign_item(payload={"technicianCrmIds": ["10"], "authorizedRemovalCrmIds": []}),
            )
        finally:
            client.close()

        assert st_id == "555"

    @respx.mock
    def test_no_eligible_appointment_raises_a_clear_error(self, st_settings: Settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _appointments_route(st_settings, [])

        client = ServiceTitanClient(st_settings)
        try:
            with pytest.raises(ValueError, match="no ServiceTitan appointment found for job 9001"):
                perform_profitwizard_item(
                    client,
                    _assign_item(
                        payload={"technicianCrmIds": ["10"], "authorizedRemovalCrmIds": []}
                    ),
                )
        finally:
            client.close()

    @respx.mock
    def test_an_unauthorized_currently_assigned_technician_is_never_removed(
        self, st_settings: Settings
    ) -> None:
        """The safety-critical invariant: a technician who is currently
        assigned, is NOT in the intended set, but was never named in
        `authorizedRemovalCrmIds`, must stay assigned. No unassign route is
        registered — a call to it fails the test."""
        mock_auth_token(st_settings.auth_url)
        _appointments_route(
            st_settings, [{"id": 555, "start": "2026-01-01T10:00:00Z", "status": "Scheduled"}]
        )
        _assignments_route(st_settings, [_assignment_row(555, 10), _assignment_row(555, 99)])
        assign_route = _assign_route(st_settings)

        client = ServiceTitanClient(st_settings)
        try:
            perform_profitwizard_item(
                client,
                _assign_item(
                    payload={
                        "technicianCrmIds": ["10", "20"],
                        "authorizedRemovalCrmIds": [],
                    }
                ),
            )
        finally:
            client.close()

        assert json.loads(assign_route.calls.last.request.content) == {
            "jobAppointmentId": 555,
            "technicianIds": [20],
        }

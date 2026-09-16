"""Tests for the MCP server helpers and representative tools."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from st_cli.dates import apply_date_params
from st_cli.exceptions import (
    APIError,
    AuthError,
    ConfigError,
    DateParseError,
    NotFoundError,
    RateLimitError,
)
from st_cli.mcp_server import (
    _handle_errors,
    _paginate,
)
from tests.conftest import make_envelope

# ---------------------------------------------------------------------------
# _paginate
# ---------------------------------------------------------------------------


class TestPaginate:
    def test_single_page_mode(self, mock_client):
        mock_client.get.return_value = make_envelope([{"id": 1}, {"id": 2}], page=2, total=10)
        result = _paginate(mock_client, "crm", "customers", page=2, page_size=50)
        assert result["data"] == [{"id": 1}, {"id": 2}]
        assert result["totalCount"] == 10
        assert result["truncated"] is False
        # Verify page was passed through
        call_params = mock_client.get.call_args[1]["params"]
        assert call_params["page"] == 2

    def test_auto_paginate_collects_all(self, mock_client):
        mock_client.get.side_effect = [
            make_envelope([{"id": 1}], has_more=True),
            make_envelope([{"id": 2}], has_more=False),
        ]
        result = _paginate(mock_client, "crm", "customers", max_results=200)
        assert len(result["data"]) == 2
        assert result["truncated"] is False

    def test_auto_paginate_truncates_at_max(self, mock_client):
        # Return 3 records but cap at 2
        mock_client.get.side_effect = [
            make_envelope([{"id": 1}, {"id": 2}], has_more=True),
            make_envelope([{"id": 3}], has_more=False),
        ]
        result = _paginate(mock_client, "crm", "customers", max_results=2)
        assert len(result["data"]) == 2
        assert result["truncated"] is True

    def test_max_results_capped_at_1000(self, mock_client):
        """Even if caller passes max_results=5000, it gets capped to 1000."""
        mock_client.get.side_effect = [
            make_envelope([{"id": i} for i in range(50)], has_more=False),
        ]
        result = _paginate(mock_client, "crm", "customers", max_results=5000)
        # Should succeed without error (cap applied internally)
        assert result["truncated"] is False

    def test_empty_results(self, mock_client):
        mock_client.get.return_value = make_envelope([])
        result = _paginate(mock_client, "crm", "customers")
        assert result["data"] == []
        assert result["truncated"] is False

    def test_params_passed_through(self, mock_client):
        mock_client.get.return_value = make_envelope([])
        _paginate(mock_client, "crm", "customers", params={"name": "Acme"}, page=1)
        call_params = mock_client.get.call_args[1]["params"]
        assert call_params["name"] == "Acme"


# ---------------------------------------------------------------------------
# _apply_created_date_params
# ---------------------------------------------------------------------------


class TestApplyDateParams:
    """Tests for the shared apply_date_params helper (moved from mcp_server to dates)."""

    def test_explicit_dates_override_range(self):
        params: dict = {}
        apply_date_params(params, "last-week", "2025-01-01", "2025-01-31")
        assert params["createdOnOrAfter"] == "2025-01-01"
        assert params["createdBefore"] == "2025-01-31"

    def test_range_string_parsed(self):
        params: dict = {}
        apply_date_params(params, "today", None, None)
        assert "createdOnOrAfter" in params
        assert "createdBefore" in params

    def test_no_params_no_change(self):
        params: dict = {}
        apply_date_params(params, None, None, None)
        assert params == {}

    def test_partial_explicit_skips_range(self):
        params: dict = {}
        apply_date_params(params, "last-week", "2025-06-01", None)
        assert params["createdOnOrAfter"] == "2025-06-01"
        assert "createdBefore" not in params

    def test_invalid_range_raises(self):
        params: dict = {}
        with pytest.raises(DateParseError):
            apply_date_params(params, "not-a-range", None, None)

    def test_custom_keys_for_dispatch(self):
        params: dict = {}
        apply_date_params(
            params,
            "this-week",
            "2025-03-01",
            "2025-03-07",
            start_key="startsOnOrAfter",
            end_key="startsBefore",
        )
        assert params["startsOnOrAfter"] == "2025-03-01"
        assert params["startsBefore"] == "2025-03-07"

    def test_dispatch_range_parsed(self):
        params: dict = {}
        apply_date_params(
            params,
            "today",
            None,
            None,
            start_key="startsOnOrAfter",
            end_key="startsBefore",
        )
        assert "startsOnOrAfter" in params
        assert "startsBefore" in params


# ---------------------------------------------------------------------------
# _handle_errors
# ---------------------------------------------------------------------------


class TestHandleErrors:
    def _make_decorated(self, exc: Exception):
        @_handle_errors
        def boom():
            raise exc

        return boom

    def test_not_found(self):
        fn = self._make_decorated(NotFoundError("customer 99"))
        with pytest.raises(ToolError, match="Not found"):
            fn()

    def test_rate_limit(self):
        fn = self._make_decorated(RateLimitError())
        with pytest.raises(ToolError, match="Rate limited"):
            fn()

    def test_auth_error(self):
        fn = self._make_decorated(AuthError("bad token"))
        with pytest.raises(ToolError, match="Authentication error"):
            fn()

    def test_config_error(self):
        fn = self._make_decorated(ConfigError("missing key"))
        with pytest.raises(ToolError, match="Configuration error"):
            fn()

    def test_date_parse_error(self):
        fn = self._make_decorated(DateParseError("nope"))
        with pytest.raises(ToolError, match="Invalid date range"):
            fn()

    def test_generic_api_error(self):
        fn = self._make_decorated(APIError(500, "server down"))
        with pytest.raises(ToolError, match="API error.*500"):
            fn()

    def test_success_passes_through(self):
        @_handle_errors
        def ok():
            return {"result": True}

        assert ok() == {"result": True}


# ---------------------------------------------------------------------------
# Representative tool tests (using mock client via lifespan context)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_ctx(mock_client):
    """Build a fake Context with lifespan_context holding the mock client."""
    ctx = MagicMock()
    ctx.lifespan_context = {"client": mock_client}
    return ctx


class TestCRMTools:
    def test_customers_list(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_crm_customers_list

        mock_client.get.return_value = make_envelope([{"id": 1, "name": "Acme"}])
        result = st_crm_customers_list(mock_ctx, name="Acme")
        assert result["data"][0]["name"] == "Acme"
        call_params = mock_client.get.call_args[1]["params"]
        assert call_params["name"] == "Acme"

    def test_customers_get(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_crm_customers_get

        mock_client.get.return_value = {"id": 42, "name": "Test"}
        result = st_crm_customers_get(mock_ctx, customer_id=42)
        assert result["id"] == 42
        mock_client.get.assert_called_once_with("crm", "customers/42")

    def test_customers_create(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_crm_customers_create

        mock_client.post.return_value = {"id": 99, "name": "New"}
        result = st_crm_customers_create(mock_ctx, data={"name": "New"})
        assert result["id"] == 99
        mock_client.post.assert_called_once_with("crm", "customers", json_body={"name": "New"})

    def test_bookings_list(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_crm_bookings_list

        mock_client.get.return_value = make_envelope([{"id": 1}])
        result = st_crm_bookings_list(mock_ctx)
        assert len(result["data"]) == 1

    def test_contacts_get(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_crm_contacts_get

        mock_client.get.return_value = {"id": 10, "type": "Phone"}
        result = st_crm_contacts_get(mock_ctx, contact_id=10)
        assert result["type"] == "Phone"


class TestJPMTools:
    def test_jobs_list_with_filters(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_jpm_jobs_list

        mock_client.get.return_value = make_envelope([{"id": 1}])
        result = st_jpm_jobs_list(mock_ctx, status="Completed", customer_id=42, date_range="today")
        assert len(result["data"]) == 1
        call_params = mock_client.get.call_args[1]["params"]
        assert call_params["jobStatus"] == "Completed"
        assert call_params["customerId"] == 42
        assert "createdOnOrAfter" in call_params

    def test_jobs_list_with_sort(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_jpm_jobs_list

        mock_client.get.return_value = make_envelope([{"id": 1}])
        # `-Id`, not `-completedOn`: jpm/jobs sorts only by Id, ModifiedOn,
        # CreatedOn or Priority and 400s on anything else.
        st_jpm_jobs_list(mock_ctx, sort="-Id")
        assert mock_client.get.call_args[1]["params"]["sort"] == "-Id"

    def test_jobs_get(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_jpm_jobs_get

        mock_client.get.return_value = {"id": 5, "number": "J-100"}
        result = st_jpm_jobs_get(mock_ctx, job_id=5)
        assert result["number"] == "J-100"

    def test_jobs_create(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_jpm_jobs_create

        body = {"customerId": 1, "jobTypeId": 2}
        mock_client.post.return_value = {"id": 10}
        result = st_jpm_jobs_create(mock_ctx, data=body)
        assert result["id"] == 10

    def test_jobs_cancel(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_jpm_jobs_cancel

        mock_client.post.return_value = None
        result = st_jpm_jobs_cancel(mock_ctx, job_id=7)
        assert result["status"] == "cancelled"
        assert result["jobId"] == 7

    def test_appointments_list(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_jpm_appointments_list

        mock_client.get.return_value = make_envelope([{"id": 1, "jobId": 5}])
        st_jpm_appointments_list(mock_ctx, job_id=5)
        call_params = mock_client.get.call_args[1]["params"]
        assert call_params["jobId"] == 5

    def test_projects_get(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_jpm_projects_get

        mock_client.get.return_value = {"id": 3, "name": "Big Project"}
        result = st_jpm_projects_get(mock_ctx, project_id=3)
        assert result["name"] == "Big Project"


class TestDispatchTools:
    def test_shifts_list(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_dispatch_shifts_list

        mock_client.get.return_value = make_envelope([{"id": 1}])
        result = st_dispatch_shifts_list(mock_ctx, date_range="today")
        assert len(result["data"]) == 1
        call_params = mock_client.get.call_args[1]["params"]
        assert "startsOnOrAfter" in call_params

    def test_who_busy(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_dispatch_who_busy

        shift = {
            "id": 1,
            "technicianId": 100,
            "technicianName": "John",
            "start": "2025-01-15T08:00:00",
            "end": "2025-01-15T17:00:00",
        }
        appt = {
            "id": 10,
            "technicianId": 100,
            "start": "2025-01-15T09:00:00",
            "end": "2025-01-15T11:00:00",
        }
        mock_client.get.side_effect = [
            make_envelope([shift]),  # shifts
            make_envelope([appt]),  # appointments
        ]
        result = st_dispatch_who_busy(mock_ctx, date_range="today")
        assert len(result["data"]) == 1
        row = result["data"][0]
        assert row["technicianId"] == 100
        assert row["shiftHours"] == 9.0
        assert row["bookedHours"] == 2.0

    def test_events_create(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_dispatch_events_create

        mock_client.post.return_value = {"id": 1, "name": "Training"}
        result = st_dispatch_events_create(mock_ctx, data={"name": "Training"})
        assert result["name"] == "Training"


class TestAccountingTools:
    def test_invoices_list(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_accounting_invoices_list

        mock_client.get.return_value = make_envelope([{"id": 1, "total": 500}])
        st_accounting_invoices_list(mock_ctx, status="Pending")
        call_params = mock_client.get.call_args[1]["params"]
        assert call_params["status"] == "Pending"

    def test_invoices_get(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_accounting_invoices_get

        mock_client.get.return_value = {"id": 1, "total": 500}
        result = st_accounting_invoices_get(mock_ctx, invoice_id=1)
        assert result["total"] == 500

    def test_payments_list(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_accounting_payments_list

        mock_client.get.return_value = make_envelope([{"id": 1}])
        result = st_accounting_payments_list(mock_ctx)
        assert len(result["data"]) == 1

    def test_payments_list_with_date_filter(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_accounting_payments_list

        mock_client.get.return_value = make_envelope([{"id": 1}])
        st_accounting_payments_list(mock_ctx, date_range="this-month")
        call_params = mock_client.get.call_args[1]["params"]
        assert "createdOnOrAfter" in call_params


class TestMembershipTools:
    def test_memberships_list(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_memberships_list

        mock_client.get.return_value = make_envelope([{"id": 1}])
        result = st_memberships_list(mock_ctx)
        assert len(result["data"]) == 1

    def test_memberships_get(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_memberships_get

        mock_client.get.return_value = {"id": 5, "status": "Active"}
        result = st_memberships_get(mock_ctx, membership_id=5)
        assert result["status"] == "Active"

    def test_types_list(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_memberships_types_list

        mock_client.get.return_value = make_envelope([{"id": 1, "name": "Gold"}])
        result = st_memberships_types_list(mock_ctx)
        assert result["data"][0]["name"] == "Gold"


# ---------------------------------------------------------------------------
# Error propagation through tools
# ---------------------------------------------------------------------------


class TestToolErrorPropagation:
    def test_not_found_becomes_tool_error(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_crm_customers_get

        mock_client.get.side_effect = NotFoundError("customer 99")
        with pytest.raises(ToolError, match="Not found"):
            st_crm_customers_get(mock_ctx, customer_id=99)

    def test_rate_limit_becomes_tool_error(self, mock_ctx, mock_client):
        from st_cli.mcp_server import st_jpm_jobs_list

        mock_client.get.side_effect = RateLimitError()
        with pytest.raises(ToolError, match="Rate limited"):
            st_jpm_jobs_list(mock_ctx)

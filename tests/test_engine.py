"""Tests for the registry-driven command/tool engine.

CLI commands are exercised end-to-end through the real ``st`` app (so these
also cover the main.py wiring); MCP tools are exercised by calling the engine
factories directly with a mock client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from st_cli import engine
from st_cli.engine import McpDeps, _is_primary, _parse_filters, _tool_name
from st_cli.registry import Action, Module, Resource
from tests.conftest import make_envelope

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_parse_filters_coerces_types(self):
        params = _parse_filters(["active=true", "page=3", "name=Acme", "n=-5"])
        assert params == {"active": True, "page": 3, "name": "Acme", "n": -5}

    def test_parse_filters_empty(self):
        assert _parse_filters(None) == {}

    def test_parse_filters_rejects_bad_pair(self):
        import typer

        with pytest.raises(typer.BadParameter):
            _parse_filters(["noequals"])

    def test_tool_name_normalizes_hyphens(self):
        assert _tool_name("marketing-ads", "attributed-leads", "list") == (
            "st_marketing_ads_attributed_leads_list"
        )

    def test_is_primary(self):
        mod = Module(name="findings", help="x", resources=())
        assert _is_primary(mod, Resource("findings"))
        assert not _is_primary(mod, Resource("finding-assets"))


# ---------------------------------------------------------------------------
# Generated CLI commands (through the real `st` app)
# ---------------------------------------------------------------------------


class TestGeneratedListCommand:
    def test_list_routes_to_module_and_path(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([{"id": 1, "name": "Svc"}])
        result = invoke(["pricebook", "services-list"])
        assert result.exit_code == 0
        assert "Svc" in result.output
        # fetch_page -> client.get(module, path, params=...)
        args = mock_client.get.call_args[0]
        assert args[0] == "pricebook"
        assert args[1] == "services"

    def test_generic_filter(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([])
        invoke(["pricebook", "services-list", "--filter", "active=true", "--filter", "code=AB"])
        params = mock_client.get.call_args[1]["params"]
        assert params["active"] is True
        assert params["code"] == "AB"

    def test_date_range_on_dated_resource(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([])
        invoke(["inventory", "purchase-orders-list", "--range", "this-month"])
        params = mock_client.get.call_args[1]["params"]
        assert "createdOnOrAfter" in params

    def test_all_pages(self, invoke, mock_client):
        mock_client.get.side_effect = [
            make_envelope([{"id": 1}], has_more=True),
            make_envelope([{"id": 2}], has_more=False),
        ]
        result = invoke(["pricebook", "services-list", "--all"])
        assert result.exit_code == 0
        assert mock_client.get.call_count == 2


class TestGeneratedCrudCommands:
    def test_get(self, invoke, mock_client):
        mock_client.get.return_value = {"id": 5, "name": "Svc"}
        result = invoke(["pricebook", "services-get", "5"])
        assert result.exit_code == 0
        mock_client.get.assert_called_once_with("pricebook", "services/5")

    def test_create(self, invoke, mock_client):
        mock_client.post.return_value = {"id": 9}
        result = invoke(["pricebook", "services-create", "--data", '{"name": "X"}'])
        assert result.exit_code == 0
        mock_client.post.assert_called_once_with("pricebook", "services", json_body={"name": "X"})

    def test_update(self, invoke, mock_client):
        mock_client.patch.return_value = {"id": 9}
        invoke(["pricebook", "services-update", "9", "--data", '{"name": "Y"}'])
        mock_client.patch.assert_called_once_with(
            "pricebook", "services/9", json_body={"name": "Y"}
        )

    def test_delete(self, invoke, mock_client):
        mock_client.delete.return_value = None
        result = invoke(["pricebook", "services-delete", "9"])
        assert result.exit_code == 0
        assert "deleted" in result.output
        mock_client.delete.assert_called_once_with("pricebook", "services/9")


class TestGeneratedActions:
    def test_action_with_id_no_body(self, invoke, mock_client):
        mock_client.post.return_value = None
        result = invoke(["inventory", "purchase-orders-approve", "3"])
        assert result.exit_code == 0
        mock_client.post.assert_called_once_with(
            "inventory", "purchase-orders/3/approve", json_body=None
        )

    def test_action_with_id_and_body(self, invoke, mock_client):
        mock_client.post.return_value = {"status": "Sold"}
        invoke(["salestech", "estimates-sell", "7", "--data", '{"soldBy": 100}'])
        mock_client.post.assert_called_once_with(
            "sales", "estimates/7/sell", json_body={"soldBy": 100}
        )

    def test_collection_action_no_id(self, invoke, mock_client):
        mock_client.post.return_value = {"ok": True}
        invoke(
            [
                "dispatch",
                "assignments-assign-technicians",
                "--data",
                '{"jobAppointmentId": 1, "technicianIds": [2]}',
            ]
        )
        mock_client.post.assert_called_once_with(
            "dispatch",
            "appointment-assignments/assign-technicians",
            json_body={"jobAppointmentId": 1, "technicianIds": [2]},
        )

    def test_put_action(self, invoke, mock_client):
        mock_client.put.return_value = {"id": 1}
        invoke(["salestech", "estimates-put-item", "7", "--data", '{"skuId": 5}'])
        mock_client.put.assert_called_once_with(
            "sales", "estimates/7/items", json_body={"skuId": 5}
        )

    def test_delete_verb_action(self, invoke, mock_client):
        mock_client.delete.return_value = None
        invoke(["jobs", "appointments-remove-hold", "4"])
        mock_client.delete.assert_called_once_with("jpm", "appointments/4/hold", json_body=None)


class TestGeneratedExport:
    def test_export_drains_feed(self, invoke, mock_client):
        mock_client.get.side_effect = [
            {"data": [{"id": 1}], "hasMore": True, "continueFrom": "t1"},
            {"data": [{"id": 2}], "hasMore": False, "continueFrom": "t2"},
        ]
        result = invoke(["telecom", "export-calls", "--all"])
        assert result.exit_code == 0
        assert "continueFrom: t2" in result.output


# ---------------------------------------------------------------------------
# Generated MCP tools (via the engine factories directly)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mcp_ctx(mock_client):
    ctx = MagicMock()
    ctx.lifespan_context = {"client": mock_client}
    return ctx


@pytest.fixture()
def deps():
    from st_cli.mcp_server import _get_client, _handle_errors, _paginate

    return McpDeps(_get_client, _paginate, _handle_errors, 1000, 200)


class TestMcpFactories:
    def test_list_tool(self, deps, mcp_ctx, mock_client):
        res = Resource("services", ops="L")
        tool = engine._make_list_tool("pricebook", res, deps)
        mock_client.get.return_value = make_envelope([{"id": 1}])
        out = tool(mcp_ctx, filters={"active": True})
        assert out["data"] == [{"id": 1}]
        assert mock_client.get.call_args[0][:2] == ("pricebook", "services")
        assert mock_client.get.call_args[1]["params"]["active"] is True

    def test_list_tool_no_default_sort_when_no_date_filter(self, deps, mcp_ctx, mock_client):
        """Resources without date_filter don't get an implicit sort — preserve API default."""
        res = Resource("services", ops="L")
        tool = engine._make_list_tool("pricebook", res, deps)
        mock_client.get.return_value = make_envelope([])
        tool(mcp_ctx)
        assert "sort" not in mock_client.get.call_args[1]["params"]

    def test_list_tool_date_filtered_defaults_to_recent_first(self, deps, mcp_ctx, mock_client):
        """Date-filterable resources default to -modifiedOn so "latest" requests work."""
        res = Resource("estimates", ops="L", date_filter=True)
        tool = engine._make_list_tool("sales", res, deps)
        mock_client.get.return_value = make_envelope([])
        tool(mcp_ctx)
        assert mock_client.get.call_args[1]["params"]["sort"] == "-modifiedOn"

    def test_list_tool_explicit_sort_wins(self, deps, mcp_ctx, mock_client):
        """Caller's `sort` arg overrides the date-filter default."""
        res = Resource("estimates", ops="L", date_filter=True)
        tool = engine._make_list_tool("sales", res, deps)
        mock_client.get.return_value = make_envelope([])
        tool(mcp_ctx, sort="+createdOn")
        assert mock_client.get.call_args[1]["params"]["sort"] == "+createdOn"

    def test_list_tool_sort_in_filters_wins_over_arg(self, deps, mcp_ctx, mock_client):
        """`filters={'sort': ...}` takes precedence over the `sort` argument and the default."""
        res = Resource("estimates", ops="L", date_filter=True)
        tool = engine._make_list_tool("sales", res, deps)
        mock_client.get.return_value = make_envelope([])
        tool(mcp_ctx, filters={"sort": "+id"}, sort="-modifiedOn")
        assert mock_client.get.call_args[1]["params"]["sort"] == "+id"

    def test_list_tool_resource_default_sort_overrides_modified_on(
        self, deps, mcp_ctx, mock_client
    ):
        """Resources with default_sort use it instead of -modifiedOn (e.g.
        payroll/gross-pay-items)."""
        res = Resource("gross-pay-items", ops="L", date_filter=True, default_sort="-date")
        tool = engine._make_list_tool("payroll", res, deps)
        mock_client.get.return_value = make_envelope([])
        tool(mcp_ctx)
        assert mock_client.get.call_args[1]["params"]["sort"] == "-date"

    def test_list_tool_explicit_sort_wins_over_resource_default(self, deps, mcp_ctx, mock_client):
        """Caller-supplied sort still wins over the resource's default_sort."""
        res = Resource("gross-pay-items", ops="L", date_filter=True, default_sort="-date")
        tool = engine._make_list_tool("payroll", res, deps)
        mock_client.get.return_value = make_envelope([])
        tool(mcp_ctx, sort="+date")
        assert mock_client.get.call_args[1]["params"]["sort"] == "+date"

    def test_get_tool(self, deps, mcp_ctx, mock_client):
        tool = engine._make_get_tool("pricebook", Resource("services"), deps)
        mock_client.get.return_value = {"id": 5}
        assert tool(mcp_ctx, record_id=5) == {"id": 5}
        mock_client.get.assert_called_once_with("pricebook", "services/5")

    def test_create_tool(self, deps, mcp_ctx, mock_client):
        tool = engine._make_create_tool("pricebook", Resource("services"), deps)
        mock_client.post.return_value = {"id": 1}
        tool(mcp_ctx, data={"name": "X"})
        mock_client.post.assert_called_once_with("pricebook", "services", json_body={"name": "X"})

    def test_delete_tool_returns_status(self, deps, mcp_ctx, mock_client):
        tool = engine._make_delete_tool("pricebook", Resource("services"), deps)
        out = tool(mcp_ctx, record_id=9)
        assert out == {"status": "deleted", "id": 9}

    def test_action_tool_with_body(self, deps, mcp_ctx, mock_client):
        action = Action("sell", needs_body=True)
        tool = engine._make_action_tool("salestech", Resource("estimates"), action, deps)
        mock_client.post.return_value = {"status": "Sold"}
        out = tool(mcp_ctx, record_id=7, data={"soldBy": 1})
        assert out == {"status": "Sold"}
        mock_client.post.assert_called_once_with(
            "salestech", "estimates/7/sell", json_body={"soldBy": 1}
        )

    def test_action_tool_none_result_becomes_ok(self, deps, mcp_ctx, mock_client):
        action = Action("unsell")
        tool = engine._make_action_tool("salestech", Resource("estimates"), action, deps)
        mock_client.post.return_value = None
        assert tool(mcp_ctx, record_id=7) == {"status": "ok"}

    def test_export_tool(self, deps, mcp_ctx, mock_client):
        tool = engine._make_export_tool("telecom", "calls", deps)
        mock_client.get.return_value = {"data": [{"id": 1}], "hasMore": False, "continueFrom": "z"}
        out = tool(mcp_ctx)
        assert out["data"] == [{"id": 1}]
        assert out["continueFrom"] == "z"

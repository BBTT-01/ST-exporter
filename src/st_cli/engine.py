"""Turn the declarative :mod:`st_cli.registry` into CLI commands and MCP tools.

The two generators below produce, per :class:`~st_cli.registry.Resource`, the
same set of operations the hand-written modules expose — but from one spec.
Each generated callable is a distinct closure with a *fixed* signature, so both
Typer (CLI) and FastMCP (tools) can introspect it normally; no dynamic
signature construction is involved.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional, cast

import typer
from fastmcp import Context

from st_cli.dates import apply_date_params
from st_cli.output import render, render_single
from st_cli.pagination import fetch_all, fetch_export_all, fetch_page
from st_cli.registry import Action, Module, Resource

_EXPORT_COLUMNS = (("ID", "id"), ("Active", "active"), ("Modified", "modifiedOn"))

_VERB_HELP = {
    "POST": "Run",
    "PATCH": "Update",
    "PUT": "Replace",
    "DELETE": "Remove",
}


def _title(resource: Resource) -> str:
    return resource.title or resource.slug.replace("-", " ").title()


def _is_primary(module: Module, resource: Resource) -> bool:
    """A resource named after its module gets bare command/tool names.

    e.g. ``st memberships list`` not ``st memberships memberships-list``;
    ``st_findings_list`` not ``st_findings_findings_list``. Matches the
    existing hand-written convention for a group's primary resource.
    """
    return resource.slug in (module.name, module.cli_group)


def _tool_name(module: str, *parts: str) -> str:
    """Build an MCP tool name like ``st_marketing_ads_attributed_leads_list``."""
    chunks = [module, *parts]
    return "st_" + "_".join(c.replace("-", "_") for c in chunks)


def _coerce(value: str) -> Any:
    """Light type coercion for ``--filter key=value`` values."""
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _parse_filters(pairs: Optional[list[str]]) -> dict[str, Any]:
    """Parse repeated ``key=value`` strings into a query-param dict."""
    params: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise typer.BadParameter(f"--filter expects key=value, got {pair!r}")
        key, _, val = pair.partition("=")
        params[key.strip()] = _coerce(val.strip())
    return params


# ===========================================================================
# CLI command factories
# ===========================================================================


def _make_list_cmd(module: str, resource: Resource) -> Callable[..., None]:
    cols = list(resource.columns)
    path = resource.api_path
    title = _title(resource)
    start_key, end_key = resource.date_keys
    has_date = resource.date_filter

    def _cmd(
        ctx: typer.Context,
        filter_: Optional[list[str]] = typer.Option(
            None, "--filter", help="Query filter key=value (repeatable)"
        ),
        range_val: Optional[str] = typer.Option(
            None, "--range", help="Date range, e.g. last-week, this-month"
        ),
        from_date: Optional[str] = typer.Option(None, "--from-date", help="Start (YYYY-MM-DD)"),
        to_date: Optional[str] = typer.Option(None, "--to-date", help="End (YYYY-MM-DD)"),
        page: int = typer.Option(1, help="Page number"),
        page_size: int = typer.Option(50, help="Page size"),
        all_pages: bool = typer.Option(False, "--all", help="Fetch all pages"),
    ) -> None:
        client = ctx.obj["client"]
        as_json = ctx.obj["json"]
        params = _parse_filters(filter_)
        if has_date:
            apply_date_params(
                params, range_val, from_date, to_date, start_key=start_key, end_key=end_key
            )
        if all_pages:
            data = list(fetch_all(client, module, path, params, page_size=page_size))
            render(data, cols, as_json=as_json, title=title)
        else:
            envelope = fetch_page(client, module, path, params, page=page, page_size=page_size)
            render(
                envelope.get("data", []),
                cols,
                as_json=as_json,
                title=title,
                total_count=envelope.get("totalCount"),
            )

    _cmd.__doc__ = f"List {title.lower()}."
    return _cmd


def _make_get_cmd(module: str, resource: Resource) -> Callable[..., None]:
    cols = list(resource.columns)
    path = resource.api_path
    label = _title(resource)

    def _cmd(
        ctx: typer.Context,
        record_id: int = typer.Argument(..., help=f"{label} ID"),
    ) -> None:
        client = ctx.obj["client"]
        data = client.get(module, f"{path}/{record_id}")
        render_single(data, cols, as_json=ctx.obj["json"])

    _cmd.__doc__ = f"Get a single {label.lower()} by ID."
    return _cmd


def _make_create_cmd(module: str, resource: Resource) -> Callable[..., None]:
    cols = list(resource.columns)
    path = resource.api_path
    label = _title(resource)

    def _cmd(
        ctx: typer.Context,
        data: str = typer.Option(..., "--data", help="Record data as JSON"),
    ) -> None:
        client = ctx.obj["client"]
        result = client.post(module, path, json_body=json.loads(data))
        render_single(result or {}, cols, as_json=ctx.obj["json"])

    _cmd.__doc__ = f"Create a {label.lower()}."
    return _cmd


def _make_write_cmd(module: str, resource: Resource, *, verb: str) -> Callable[..., None]:
    """Generate an update (PATCH) or replace (PUT) command."""
    cols = list(resource.columns)
    path = resource.api_path
    label = _title(resource)
    method = "patch" if verb == "PATCH" else "put"
    word = "Update" if verb == "PATCH" else "Replace"

    def _cmd(
        ctx: typer.Context,
        record_id: int = typer.Argument(..., help=f"{label} ID"),
        data: str = typer.Option(..., "--data", help="Fields as JSON"),
    ) -> None:
        client = ctx.obj["client"]
        result = getattr(client, method)(module, f"{path}/{record_id}", json_body=json.loads(data))
        render_single(result or {}, cols, as_json=ctx.obj["json"])

    _cmd.__doc__ = f"{word} a {label.lower()}."
    return _cmd


def _make_delete_cmd(module: str, resource: Resource) -> Callable[..., None]:
    path = resource.api_path
    label = _title(resource)

    def _cmd(
        ctx: typer.Context,
        record_id: int = typer.Argument(..., help=f"{label} ID"),
    ) -> None:
        client = ctx.obj["client"]
        client.delete(module, f"{path}/{record_id}")
        typer.echo(f"{label} {record_id} deleted.")

    _cmd.__doc__ = f"Delete a {label.lower()} by ID."
    return _cmd


def _make_action_cmd(module: str, resource: Resource, action: Action) -> Callable[..., None]:
    path = resource.api_path
    label = _title(resource)
    method = action.verb.lower()
    desc = action.help or f"{_VERB_HELP.get(action.verb, 'Run')} {action.name} on {label.lower()}"

    def _resource_url(record_id: int | None) -> str:
        if action.needs_id and record_id is not None:
            return f"{path}/{record_id}/{action.subpath}"
        return f"{path}/{action.subpath}"

    def _call(client: Any, url: str, body: dict[str, Any] | None) -> Any:
        if method == "delete":
            return client.delete(module, url, json_body=body)
        return getattr(client, method)(module, url, json_body=body)

    if action.needs_id and action.needs_body:

        def _cmd(
            ctx: typer.Context,
            record_id: int = typer.Argument(..., help=f"{label} ID"),
            data: str = typer.Option(..., "--data", help="Action body as JSON"),
        ) -> None:
            client = ctx.obj["client"]
            result = _call(client, _resource_url(record_id), json.loads(data))
            _echo_action(ctx, result, f"{label} {record_id}: {action.name} done.")

    elif action.needs_id:

        def _cmd(  # type: ignore[misc]
            ctx: typer.Context,
            record_id: int = typer.Argument(..., help=f"{label} ID"),
        ) -> None:
            client = ctx.obj["client"]
            result = _call(client, _resource_url(record_id), None)
            _echo_action(ctx, result, f"{label} {record_id}: {action.name} done.")

    elif action.needs_body:

        def _cmd(  # type: ignore[misc]
            ctx: typer.Context,
            data: str = typer.Option(..., "--data", help="Action body as JSON"),
        ) -> None:
            client = ctx.obj["client"]
            result = _call(client, _resource_url(None), json.loads(data))
            _echo_action(ctx, result, f"{label}: {action.name} done.")

    else:

        def _cmd(ctx: typer.Context) -> None:  # type: ignore[misc]
            client = ctx.obj["client"]
            result = _call(client, _resource_url(None), None)
            _echo_action(ctx, result, f"{label}: {action.name} done.")

    _cmd.__doc__ = desc
    return _cmd


def _echo_action(ctx: typer.Context, result: Any, message: str) -> None:
    """Render an action result: JSON if it returned a body, else a confirmation."""
    if not result:
        typer.echo(message)
        return
    if ctx.obj["json"]:
        from st_cli.output import console

        console.print_json(json.dumps(result, default=str))
    elif isinstance(result, dict):
        render_single(result, [("Result", "")], as_json=False)
    else:
        typer.echo(str(result))


def _make_export_cmd(module: str, feed: str) -> Callable[..., None]:
    def _cmd(
        ctx: typer.Context,
        from_token: Optional[str] = typer.Option(
            None, "--from", help="continueFrom token to resume a feed"
        ),
        max_records: int = typer.Option(1000, "--max", help="Stop after N records"),
        all_records: bool = typer.Option(False, "--all", help="Drain the entire feed"),
    ) -> None:
        client = ctx.obj["client"]
        cap = None if all_records else max_records
        records, token = fetch_export_all(client, module, feed, max_records=cap)
        as_json = ctx.obj["json"]
        if as_json:
            from st_cli.output import console

            console.print_json(json.dumps({"data": records, "continueFrom": token}, default=str))
            return
        render(records, list(_EXPORT_COLUMNS), as_json=False, title=f"Export: {feed}")
        typer.echo(f"continueFrom: {token}")

    _cmd.__doc__ = f"Read the {feed} export change-feed."
    return _cmd


def add_resource_commands(app: typer.Typer, module: Module) -> None:
    """Register every generated command for *module* onto an existing Typer app."""
    for resource in module.resources:
        base = "" if _is_primary(module, resource) else f"{resource.slug}-"
        if "L" in resource.ops:
            app.command(f"{base}list")(_make_list_cmd(module.name, resource))
        if "R" in resource.ops:
            app.command(f"{base}get")(_make_get_cmd(module.name, resource))
        if "C" in resource.ops:
            app.command(f"{base}create")(_make_create_cmd(module.name, resource))
        if "U" in resource.ops:
            app.command(f"{base}update")(_make_write_cmd(module.name, resource, verb="PATCH"))
        if "P" in resource.ops:
            app.command(f"{base}replace")(_make_write_cmd(module.name, resource, verb="PUT"))
        if "D" in resource.ops:
            app.command(f"{base}delete")(_make_delete_cmd(module.name, resource))
        for action in resource.actions:
            app.command(f"{base}{action.name}")(_make_action_cmd(module.name, resource, action))
    for feed in module.export_feeds:
        app.command(f"export-{feed}")(_make_export_cmd(module.name, feed))


def build_cli_app(module: Module) -> typer.Typer:
    """Build a fresh Typer command group for a brand-new module."""
    app = typer.Typer(help=module.help)
    add_resource_commands(app, module)
    return app


# ===========================================================================
# MCP tool factories
# ===========================================================================


class McpDeps:
    """Shared FastMCP helpers injected by the server (avoids CLI→FastMCP import)."""

    def __init__(
        self,
        get_client: Callable[[Any], Any],
        paginate: Callable[..., dict[str, Any]],
        handle_errors: Callable[[Callable[..., Any]], Callable[..., Any]],
        max_results_cap: int,
        default_max_results: int,
    ) -> None:
        self.get_client = get_client
        self.paginate = paginate
        self.handle_errors = handle_errors
        self.max_results_cap = max_results_cap
        self.default_max_results = default_max_results


def _make_list_tool(module: str, resource: Resource, deps: McpDeps) -> Callable[..., Any]:
    path = resource.api_path
    label = _title(resource)
    start_key, end_key = resource.date_keys
    has_date = resource.date_filter
    # Per-resource override (e.g. payroll/gross-pay-items only accepts ±date).
    default_sort = resource.default_sort or ("-modifiedOn" if has_date else None)
    default_max = deps.default_max_results

    def _tool(
        ctx: Context,
        filters: dict[str, Any] | None = None,
        sort: str | None = None,
        date_range: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        page: int | None = None,
        page_size: int = 50,
        max_results: int = default_max,
    ) -> dict[str, Any]:
        params = dict(filters or {})
        if has_date:
            apply_date_params(
                params,
                date_range,
                created_after,
                created_before,
                start_key=start_key,
                end_key=end_key,
            )
        # ServiceTitan list endpoints have no documented default order and observe
        # ascending-by-id in practice — so "latest" requests return the oldest
        # records first. Default to newest-first so "give me the latest X" works
        # as a human reads it. Explicit sort (param or filters) always wins.
        if "sort" not in params:
            if sort is not None:
                params["sort"] = sort
            elif default_sort is not None:
                params["sort"] = default_sort
        return deps.paginate(
            deps.get_client(ctx), module, path, params, page, page_size, max_results
        )

    sort_note = (
        " Sorted by `-modifiedOn` (newest first) by default; pass `sort` "
        "(e.g. `+createdOn`) or `filters={'sort': ...}` to override."
        if has_date
        else " Pass `sort` (e.g. `-modifiedOn`) for explicit ordering."
    )
    _tool.__doc__ = f"List {label.lower()}. Optional `filters` dict of query params.{sort_note}"
    return _tool


def _make_get_tool(module: str, resource: Resource, deps: McpDeps) -> Callable[..., Any]:
    path = resource.api_path
    label = _title(resource)

    def _tool(ctx: Context, record_id: int) -> dict[str, Any]:
        return cast(dict[str, Any], deps.get_client(ctx).get(module, f"{path}/{record_id}"))

    _tool.__doc__ = f"Get a single {label.lower()} by ID."
    return _tool


def _make_create_tool(module: str, resource: Resource, deps: McpDeps) -> Callable[..., Any]:
    path = resource.api_path
    label = _title(resource)

    def _tool(ctx: Context, data: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], deps.get_client(ctx).post(module, path, json_body=data))

    _tool.__doc__ = f"Create a {label.lower()}."
    return _tool


def _make_write_tool(
    module: str, resource: Resource, deps: McpDeps, *, verb: str
) -> Callable[..., Any]:
    path = resource.api_path
    label = _title(resource)
    method = "patch" if verb == "PATCH" else "put"
    word = "Update" if verb == "PATCH" else "Replace"

    def _tool(ctx: Context, record_id: int, data: dict[str, Any]) -> dict[str, Any]:
        client = deps.get_client(ctx)
        return cast(
            dict[str, Any], getattr(client, method)(module, f"{path}/{record_id}", json_body=data)
        )

    _tool.__doc__ = f"{word} a {label.lower()}."
    return _tool


def _make_delete_tool(module: str, resource: Resource, deps: McpDeps) -> Callable[..., Any]:
    path = resource.api_path
    label = _title(resource)

    def _tool(ctx: Context, record_id: int) -> dict[str, Any]:
        deps.get_client(ctx).delete(module, f"{path}/{record_id}")
        return {"status": "deleted", "id": record_id}

    _tool.__doc__ = f"Delete a {label.lower()} by ID."
    return _tool


def _make_action_tool(
    module: str, resource: Resource, action: Action, deps: McpDeps
) -> Callable[..., Any]:
    path = resource.api_path
    label = _title(resource)
    method = action.verb.lower()
    desc = action.help or f"{action.name} on {label.lower()}"

    def _url(record_id: int | None) -> str:
        if action.needs_id and record_id is not None:
            return f"{path}/{record_id}/{action.subpath}"
        return f"{path}/{action.subpath}"

    def _call(client: Any, url: str, body: dict[str, Any] | None) -> Any:
        result = getattr(client, method)(module, url, json_body=body)
        return result if result is not None else {"status": "ok"}

    if action.needs_id and action.needs_body:

        def _tool(ctx: Context, record_id: int, data: dict[str, Any]) -> Any:
            return _call(deps.get_client(ctx), _url(record_id), data)

    elif action.needs_id:

        def _tool(ctx: Context, record_id: int) -> Any:  # type: ignore[misc]
            return _call(deps.get_client(ctx), _url(record_id), None)

    elif action.needs_body:

        def _tool(ctx: Context, data: dict[str, Any]) -> Any:  # type: ignore[misc]
            return _call(deps.get_client(ctx), _url(None), data)

    else:

        def _tool(ctx: Context) -> Any:  # type: ignore[misc]
            return _call(deps.get_client(ctx), _url(None), None)

    _tool.__doc__ = desc
    return _tool


def _make_export_tool(module: str, feed: str, deps: McpDeps) -> Callable[..., Any]:
    default_max = deps.default_max_results
    cap = deps.max_results_cap

    def _tool(
        ctx: Context,
        continue_from: str | None = None,
        max_records: int = default_max,
    ) -> dict[str, Any]:
        records, token = fetch_export_all(
            deps.get_client(ctx),
            module,
            feed,
            continue_from=continue_from,
            max_records=min(max_records, cap),
        )
        return {"data": records, "continueFrom": token, "totalCount": len(records)}

    _tool.__doc__ = (
        f"Read the {feed} export change-feed. Pass `continue_from` to resume from a "
        f"prior token (the response's `continueFrom` token feeds the next poll)."
    )
    return _tool


def register_mcp_tools(mcp: Any, module: Module, deps: McpDeps) -> None:
    """Register every generated FastMCP tool for *module* on the server."""

    def _add(name: str, fn: Callable[..., Any], *, write: bool) -> None:
        tag = "write" if write else "read"
        mcp.tool(name=name, tags={module.name, tag})(deps.handle_errors(fn))

    for resource in module.resources:
        # Primary resource → omit the redundant slug (st_findings_list, not _findings_findings_list)
        parts = () if _is_primary(module, resource) else (resource.slug,)

        def _name(action: str) -> str:
            return _tool_name(module.name, *parts, action)

        if "L" in resource.ops:
            _add(_name("list"), _make_list_tool(module.name, resource, deps), write=False)
        if "R" in resource.ops:
            _add(_name("get"), _make_get_tool(module.name, resource, deps), write=False)
        if "C" in resource.ops:
            _add(_name("create"), _make_create_tool(module.name, resource, deps), write=True)
        if "U" in resource.ops:
            _add(
                _name("update"),
                _make_write_tool(module.name, resource, deps, verb="PATCH"),
                write=True,
            )
        if "P" in resource.ops:
            _add(
                _name("replace"),
                _make_write_tool(module.name, resource, deps, verb="PUT"),
                write=True,
            )
        if "D" in resource.ops:
            _add(_name("delete"), _make_delete_tool(module.name, resource, deps), write=True)
        for action in resource.actions:
            _add(
                _name(action.name),
                _make_action_tool(module.name, resource, action, deps),
                write=True,
            )
    for feed in module.export_feeds:
        _add(
            _tool_name(module.name, "export", feed),
            _make_export_tool(module.name, feed, deps),
            write=False,
        )

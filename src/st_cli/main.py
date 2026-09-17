"""Root Typer app — global flags + module registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import typer

from st_cli import engine
from st_cli.client import ServiceTitanClient
from st_cli.commands import accounting, crm, dispatch, jobs, memberships, reporting, settings
from st_cli.config import load_settings
from st_cli.exceptions import STCLIError
from st_cli.registry import EXTENSIONS, MODULES

app = typer.Typer(
    name="st",
    help="CLI for the ServiceTitan REST API v2",
    no_args_is_help=True,
)

# The 7 hand-written groups carry the bespoke, ergonomic commands (typed
# filters, who-busy, capacity, reporting). The registry generates the rest.
_HANDWRITTEN = {
    "crm": crm.app,
    "jobs": jobs.app,
    "dispatch": dispatch.app,
    "accounting": accounting.app,
    "memberships": memberships.app,
    "reporting": reporting.app,
    "settings": settings.app,
}
for _group, _existing_app in _HANDWRITTEN.items():
    app.add_typer(_existing_app, name=_group)

# Append registry-generated gap commands onto the hand-written groups …
for _ext in EXTENSIONS:
    engine.add_resource_commands(_HANDWRITTEN[_ext.cli_group], _ext)

# … and register every brand-new module group from the registry.
for _mod in MODULES:
    app.add_typer(engine.build_cli_app(_mod), name=_mod.cli_group)


class _LazyClient:
    """Defers client creation until first attribute access (skips init for --help)."""

    def __init__(self, env_file: Path | None) -> None:
        self._env_file = env_file
        self._client: ServiceTitanClient | None = None

    def _init(self) -> ServiceTitanClient:
        if self._client is None:
            try:
                settings = load_settings(self._env_file)
            except Exception as exc:
                typer.echo(f"Config error: {exc}", err=True)
                raise typer.Exit(1) from exc
            self._client = ServiceTitanClient(settings)
        return self._client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._init(), name)


@app.callback()
def main(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON instead of table"),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file"),
) -> None:
    """ServiceTitan CLI — interact with the ST API from the terminal."""
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_output
    ctx.obj["client"] = _LazyClient(env_file)


def cli() -> None:
    """Entry point for the `st` command."""
    try:
        app()
    except STCLIError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

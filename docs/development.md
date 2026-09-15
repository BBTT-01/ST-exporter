# Development

```bash
# Install dev deps into the project venv (uv)
uv pip install -e ".[dev]"

# Tests
uv run python -m pytest                                  # full suite
uv run python -m pytest tests/commands/test_dispatch.py  # one file
uv run python -m pytest -x                               # stop on first failure

# Lint + format
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run mypy src/                                         # see note below

# Export Store contract fixtures (contracts/fixtures/)
uv run python scripts/gen_contract_fixtures.py --check    # CI: is the suite stale?
uv run python scripts/gen_contract_fixtures.py            # regenerate — deliberately
```

> Regenerating the contract fixtures is not a way to make a red test green: three
> other codebases test themselves against those exact bytes. Read
> [the contract doc](export-contract.md) first — a column change needs a version
> bump and a message to the app teams.

> Run tests as `uv run python -m pytest` (not bare `uv run pytest`) so they
> always use the project venv's interpreter rather than a stray shim.

## Project layout

```
src/st_cli/
  client.py        HTTP client (get/post/patch/put/delete, auth, retry)
  auth.py          OAuth token manager + two-tier cache
  pagination.py    page cursors + export change-feed helpers
  dates.py         human date-range parser
  availability.py  shifts × appointments → availability rows
  output.py        Rich tables / JSON
  config.py        pydantic-settings (ST_* env vars)
  registry.py      declarative API spec (the single source of truth)
  engine.py        generates CLI commands + MCP tools from the registry
  main.py          `st` Typer app — wires hand-written + generated groups
  mcp_server.py    `st-mcp` FastMCP server — bespoke tools + generated tools
  commands/        the 7 hand-tuned CLI groups
scripts/
  gen_api_coverage.py   regenerates docs/api-coverage.md from the live app
tests/               mirrors the source layout
```

See [Architecture](architecture.md) for how `registry.py` + `engine.py` work and
how to add a resource.

## Testing patterns

- **HTTP level** — `respx` mocks the transport for `client.py` tests.
- **CLI** — Typer's `CliRunner` with a mocked client (the `invoke` fixture
  patches `_LazyClient`). Generated commands are tested *through the real `st`
  app*, so those tests also cover the `main.py` wiring.
- **MCP** — engine tool factories are called directly with a mock context.
- **Registry integrity** — `tests/test_registry.py` asserts unique names, valid
  ops/verbs, that no generated command shadows a hand-written one, and locks the
  estimates→`salestech` / job-types→`jpm` routing fixes.
- `make_envelope()` builds ServiceTitan-style paginated responses;
  `time-machine` is available for date-dependent tests.

## Conventions

- Line length 100; ruff rules `E, F, I, N, W`. Tests may use PascalCase mock
  names (`N806` is ignored under `tests/**`).
- mypy is configured `strict`, but the baseline carries pre-existing
  `no-any-return` findings from `client.*` returning `Any`. It is **not** a
  passing gate today; keep new code consistent with the existing style rather
  than chasing those.

## Regenerating the coverage doc

After adding resources to `registry.py` (or new hand-written commands):

```bash
uv run python scripts/gen_api_coverage.py
```

This rebuilds [API coverage](api-coverage.md) from the live command tree, so it
can't drift from the code.

---

← [Architecture](architecture.md) · [Docs index](README.md)

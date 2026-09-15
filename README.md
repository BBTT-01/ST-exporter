# Unofficial ServiceTitan CLI & MCP

An unofficial command-line tool and MCP server for the [ServiceTitan REST API v2](https://developer.servicetitan.io/). Two interfaces over the same core:

- **`st`** — Typer-based CLI with rich tables or JSON output, for humans.
- **`st-mcp`** — FastMCP server exposing the same operations as tools for AI agents over stdio.

Coverage spans **all 24 ServiceTitan API v2 modules** (~390 commands / tools). A
handful of high-traffic endpoints are hand-tuned (typed filters, `dispatch
who-busy`, `dispatch capacity`, reporting); the rest are generated from one
declarative registry. See the [full API coverage](docs/api-coverage.md).

> Not affiliated with or endorsed by ServiceTitan, Inc.

## Documentation

Full docs live in **[`docs/`](docs/README.md)**:

| Page | What's in it |
|------|--------------|
| [Installation](docs/installation.md) | One-shot installer (macOS/Linux/Windows) and manual install |
| [Configuration](docs/configuration.md) | `.env` variables, environments, OAuth/token caching, rate limits |
| [Usage](docs/usage.md) | Global options, date ranges, pagination, `--filter`, export feeds |
| [CLI reference](docs/cli-reference.md) | The 7 hand-tuned groups in depth + the generated-command pattern |
| [Examples & recipes](docs/examples.md) | Sample command per module with timeframes, CLI + MCP paired |
| [API coverage](docs/api-coverage.md) | Auto-generated inventory of every command across all 24 modules |
| [MCP server](docs/mcp-server.md) | Running `st-mcp`, tool naming, pagination model, Claude hookup |
| [Architecture](docs/architecture.md) | Registry + engine design, how to add a resource, internals |
| [Development](docs/development.md) | Tests, lint, type-check, project layout |
| [Export Store contract](docs/export-contract.md) | The contract three other codebases read: version per feed, what forces a bump, the committed fixtures, the scrubbing rule |
| [Connector workflow](docs/examples/connector-export.yml) | The caller workflow a contractor's repo runs: per-product feed flags, cadences, the one drain |

Release notes live in **[`CHANGELOG.md`](CHANGELOG.md)** — see
[0.2.0 · Full API coverage](CHANGELOG.md#020--2026-06-03--full-servicetitan-api-coverage).

### Intended integration path: Customer-Built Apps only

ServiceTitan defines three integration paths in [Customer Integration Paths](https://developer.servicetitan.io/integration-paths):

- **Certified Apps** *(Best Practice)* — built by a third party, reviewed by ServiceTitan, distributed via [marketplace.servicetitan.com](https://marketplace.servicetitan.com). Customers don't share App Keys.
- **Customer-Built Apps** *(Supported)* — *"created, hosted, and maintained by you. You can use your own App Keys to connect your integration software to ServiceTitan, without sharing those keys with external software you don't control."*
- **Tunneled Apps** *(Not Allowed)* — a third party gets access to your App Key. Identity-masking and ungoverned data access; explicitly disallowed by ServiceTitan.

**Use this repo only as a Customer-Built App** — you host it, you run it, the App Keys stay with you. ServiceTitan's prerequisites for that path:

1. Be on the **Works** or **Ent Plus** package.
2. Email **integrations@servicetitan.com** to request an Integration Environment.

> ⚠️ **Don't make this a Tunneled App.** Don't hand your `ST_APP_KEY` / `ST_CLIENT_SECRET` to a vendor or external service to run on your behalf, and don't deploy this CLI under credentials a third party owns. If a vendor asks you to create an app in your tenant and share its keys with them, that's the tunneling pattern ServiceTitan disallows — point them at the Certified Apps path instead.
>
> If you want to *distribute* a tool like this to other ServiceTitan customers, build a Certified App and go through ServiceTitan's marketplace review — this repo isn't a substitute for that.

## Quick start

```bash
# 1. Install (see docs/installation.md for the one-shot installer)
uv pip install -e ".[dev]"

# 2. Configure (see docs/configuration.md)
cat > .env <<'EOF'
ST_CLIENT_ID=...
ST_CLIENT_SECRET=...
ST_APP_KEY=...
ST_TENANT_ID=...
ST_ENVIRONMENT=production
EOF

# 3. Run some commands
st crm customers-list --name "Smith"
st jobs list --range today --json
st dispatch who-busy --range this-week
st salestech estimates-sell 12345 --data '{"soldBy": 99}'

# 4. Run the MCP server (for an AI agent client)
st-mcp
```

New to it? Read [Usage](docs/usage.md), then browse the [CLI reference](docs/cli-reference.md)
or search the [API coverage](docs/api-coverage.md). Requires Python 3.11+.

## Development

```bash
uv run python -m pytest          # tests
uv run ruff check src/ tests/    # lint
uv run ruff format src/ tests/   # format
```

See [Development](docs/development.md) for the full layout and testing patterns,
and [Architecture](docs/architecture.md) for how the registry + engine generate
the command surface.

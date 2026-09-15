# Documentation

Docs for the unofficial ServiceTitan CLI (`st`) and MCP server (`st-mcp`).
Start at the [project README](../README.md) for the overview and the
Customer-Built-App integration-path warning.

## Pages

| Page | What's in it |
|------|--------------|
| [Installation](installation.md) | One-shot installer (macOS/Linux/Windows) and manual install |
| [Configuration](configuration.md) | `.env` variables, environments, OAuth/token caching |
| [Usage](usage.md) | Global options, date ranges, pagination, quick start |
| [CLI reference](cli-reference.md) | The hand-tuned groups in depth + the generated-command pattern |
| [Examples & recipes](examples.md) | Sample command per module with timeframes, CLI + MCP paired |
| [API coverage](api-coverage.md) | Auto-generated inventory of every command across all 24 modules |
| [MCP server](mcp-server.md) | Running `st-mcp`, tool naming, pagination model, Claude hookup |
| [Architecture](architecture.md) | Registry + engine design, how to add a resource, internals |
| [Development](development.md) | Tests, lint, type-check, project layout |
| [Export Store contract](export-contract.md) | Contract version per feed, what forces a bump, the committed fixtures, how a consumer verifies itself |
| [Changelog](../CHANGELOG.md) | Release notes — what shipped in each version |

## Quick links

- **Just want to run a command?** → [Usage](usage.md) then [CLI reference](cli-reference.md)
- **Want copy-paste samples per module?** → [Examples & recipes](examples.md)
- **Looking for a specific endpoint?** → [API coverage](api-coverage.md)
- **Wiring it into Claude Code / Desktop?** → [MCP server](mcp-server.md)
- **Adding a new endpoint?** → [Architecture → Adding a resource](architecture.md#adding-a-resource)

---

← [Back to project README](../README.md)

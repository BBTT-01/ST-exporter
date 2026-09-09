# Changelog

All notable changes to `st-cli` (the `st` CLI and `st-mcp` MCP server) are
documented here. Format follows [Keep a Changelog](https://keepachangelog.com/);
this project aims for [Semantic Versioning](https://semver.org/).

## [Unreleased] · One place to bump the version

The version was written in four places — `pyproject.toml`, `EXPORTER_VERSION`,
the workflow's `EXPECTED_EXPORTER_VERSION`, and the workflow's checkout `ref:`.
Cutting 0.2.7 missed two of them in a row, and 0.2.1 shipped the wrong code
because the same bump was missed silently.

Now two, and never edited by hand:

- `EXPORTER_VERSION` reads the installed distribution metadata, so
  `pyproject.toml` is its single source. The workflow installs the code it just
  checked out, so it always describes *that* code.
- The workflow has one literal, `EXPORTER_TAG`. The checkout ref and the guard's
  expected version are both derived from it, so they cannot disagree. A tag not
  matching `exporter-vX.Y.Z` now fails the run rather than deriving nonsense.
- `scripts/release.sh <version>` bumps both files and verifies both landed.

The remaining literal stays because no *proven* GitHub context names a called
reusable workflow's own ref — `GITHUB_WORKFLOW_REF` was tried and resolved to the
caller's branch. A diagnostic step now records what `github.job_workflow_ref`
actually resolves to on a real run, so it can be removed on evidence.

## [0.2.7] — 2026-09-09 · Multi-technician jobs reach the whole crew

**The `jobs` tab is now one row per assigned technician, not one per appointment.**

ServiceTitan supports multi-technician appointments — an install crew of three is
one appointment with three live assignments. The exporter previously resolved a
single `st_technician_id` per appointment (latest `assignedOn`, ties on lowest id)
because the contract has one such column, so the rest of the crew silently lost
the job. Found on job 21465348, a three-technician install where two of the three
technicians could not see their own work.

- `_active_technician_id` → `_active_technician_ids`, returning every assigned
  technician; `build_job_rows` emits one row each.
- Removal is now resolved **per technician**. The assignment feed is append-only,
  so an unassigned technician still has a live `Active` record in it; only their
  LATEST event counts. Filtering removal rows alone would have resurrected them.
- An appointment with no assigned technician still emits one row with a null
  `st_technician_id`, unchanged.

**Column set is unchanged**, but `st_appointment_id` is no longer unique in the
tab. Consumers must key on (`st_technician_id`, `st_job_id`). Expect row counts to
grow with average crew size.

Closes the `KNOWN_UNVERIFIED.md` entry on the multi-tech tie-break rule.

## [0.2.0] — 2026-06-03 · Full ServiceTitan API coverage

The headline release: `st` and `st-mcp` now span the **entire ServiceTitan REST
API v2** — **all 24 modules**, **393 CLI commands**, and **394 MCP tools** — up
from ~40 hand-written endpoints across 7 modules. The expansion is generated
from a single declarative spec, so the surface stays consistent and cheap to
extend instead of being hand-maintained one function at a time.

### TL;DR

| | Before (0.1.x) | After (0.2.0) |
|---|---|---|
| Modules | 7 | **24** |
| CLI commands | ~40 | **393** |
| MCP tools | ~40 | **394** |
| Source of truth | hand-written per endpoint | one declarative registry |
| Tests | — | **248 passing** |

### Added

- **Registry-driven coverage of all 24 modules.** A new `registry.py` declares
  every module → resource → operation, and a new `engine.py` turns each
  `Resource` into both Typer commands and FastMCP tools from the same spec.
  Newly covered modules include: `pricebook`, `inventory`, `salestech`
  (estimates), `payroll`, `marketing`, `marketing-ads`, `timesheets`,
  `equipment-systems`, `findings`, `task-management`, `telecom`,
  `customer-interactions`, `forms`, `job-booking` (`jbce`),
  `marketing-reputation`, `scheduling-pro`, and `service-agreements` — plus
  gap operations layered onto the existing `crm`, `jobs` (`jpm`), `dispatch`,
  `accounting`, `memberships`, and `settings` groups.
- **Full CRUD + domain actions.** Beyond list/get, generated resources support
  create (`POST`), update (`PATCH`), replace (`PUT`), delete, and documented
  domain actions (e.g. `salestech estimates-sell`, `inventory
  purchase-orders-approve`, `dispatch assignments-assign-technicians`).
- **Export change-feeds.** Modules with change-feeds expose `export-<feed>`
  commands/tools that stream the dataset and return a `continueFrom` token for
  incremental resume (`pagination.fetch_export_page` / `fetch_export_all`).
- **HTTP verbs.** `client.py` gained `put()` and `delete()` (delete accepts
  query params and an optional body) to back the new write operations.
- **Generic list filtering.** Generated list commands take a repeatable
  `--filter key=value` (MCP: a `filters` dict), with light bool/int coercion.
- **Docs set.** A multi-page `docs/` tree (installation, configuration, usage,
  CLI reference, **API coverage**, MCP server, architecture, development) plus a
  per-module **[Examples & recipes](docs/examples.md)** page with timeframes and
  paired CLI ↔ MCP samples. `scripts/gen_api_coverage.py` regenerates the
  coverage inventory from the live command/tool trees.
- **Tests.** New `test_registry.py` (spec invariants, collision and routing
  checks) and `test_engine.py` (factory + generated-command behavior), alongside
  expanded client/pagination/MCP coverage — 248 tests total.

### Fixed

- **Estimates routing.** `estimates` now correctly live under `salestech`
  (`/salestech/...`), not `accounting`. The old `accounting estimates-*`
  commands/tools were removed.
- **Job types routing.** `job-types` now correctly live under `jpm`/`jobs`
  (`/jpm/...`), not `settings`. The old `settings job-types-*` command was
  removed.
- **Reporting rate-limit note** corrected to "1 of the same report per minute
  per tenant."

### Changed

- Both `main.py` (CLI) and `mcp_server.py` (MCP) are now thin wrappers that wire
  the registry through the engine; the 7 original modules keep their hand-tuned
  typed filters as bespoke escapes where the generic archetypes don't fit.
- A resource named after its module gets bare names (`st findings list`,
  `st_findings_list`) rather than a redundant slug.
- MCP tool names follow `st_{module}_{resource}_{action}`; note `jobs`→`jpm` and
  `estimates`→`salestech` in the module segment.

### Migration notes

- `st accounting estimates-list/get` → **`st salestech estimates-list/get`**
  (MCP: `st_accounting_estimates_*` → `st_salestech_estimates_*`).
- `st settings job-types-list` → **`st jobs job-types-list`**
  (MCP: `st_settings_job_types_*` → `st_jpm_job_types_*`).
- No changes to auth, configuration (`ST_*` env vars), or the seven original
  groups' existing commands.

## [0.1.0] — Initial release

- `st` Typer CLI and `st-mcp` FastMCP server over a shared core (client, auth,
  pagination, human-friendly date ranges, rich table output).
- Hand-written coverage of `crm`, `jobs`, `dispatch`, `accounting`,
  `memberships`, `reporting`, and `settings`, including dispatch availability
  (`who-busy`, `capacity`).

[0.2.0]: https://github.com/utkukaynar/Unofficial-ServiceTitan-CLI-MCP/releases/tag/v0.2.0

# Ticket 06 — Exporter Workflow, Caller Repo, Outbox Drain, SETUP.md

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `BBTT-01/ST-exporter`'s exporter runnable by a shop owner via a
reusable GitHub Actions workflow, called from a thin `BBTT-01/tr-doorservpro`
caller pinned to a version tag, draining TradeRated's CRM Outbox in the same
run, with a plain-English `SETUP.md` for the owner.

**Architecture:** Ticket 05 already built `st_exporter` as a one-shot `st-export`
command that fetches ServiceTitan feeds and writes the Export Store Sheet. This
plan (a) teaches `st-export` to run a *subset* of feeds per invocation (`--feeds
jobs` / `--feeds technicians`) so the reusable workflow can honor the spec's
different cadences (jobs ~5 min, technicians ~30 min) without wasting API calls;
(b) adds a new `st_exporter/outbox/` package that claims TradeRated's CRM
Outbox, performs each item against ServiceTitan, and reports the result back —
made crash-safe against at-least-once delivery by a small ledger tab on the
private raw-cache Sheet; (c) wires both into a `workflow_call` reusable workflow
in this repo; (d) stands up `tr-doorservpro` as the pinned caller with its own
`SETUP.md`.

**Tech Stack:** Python 3.11, Typer, httpx, pydantic-settings, gspread, pytest +
respx (existing stack — no new runtime dependencies).

**Spec:** `/Users/clay/.claude/jobs/44f9b7b9/tmp/hosted-bridge/servicetitan-hosted/spec.md`
(sections: "Export Store contract", "Outbox contract", "The exporter"),
`/Users/clay/.claude/jobs/44f9b7b9/tmp/hosted-bridge/servicetitan-hosted/issues/06-exporter-workflow-and-setup.md`,
`/Users/clay/Downloads/TradeRated Stuff/handoff-exporter.md`,
`/Users/clay/Downloads/TradeRated Stuff/START-HERE.md`. These are a local,
no-remote snapshot — treat as read-only input, do not expect edits to sync back.

## Global Constraints

- Branch `feat/servicetitan-hosted` in every repo touched (`ST-exporter`,
  `tr-doorservpro`); never commit to `main`.
- Never see, type, hold, or store a customer's ServiceTitan credentials.
- No secret in a log at any verbosity — audit new code's logging the same way
  `logging_setup.py`'s docstring already audits `st_exporter`'s.
- No scope creep: five entity groups fetched, two tabs written (`jobs`,
  `technicians`) plus `_meta`. No price-book work beyond the existing no-op
  flag. No Airtable/Supabase targets. No customers/locations tabs.
- Nothing points at TradeRated production; staging/preview only (this plan adds
  no traderatedapp code, so this mostly means: outbox base URL secrets must be
  staging URLs when they're set later in ticket 07).
- Commit locally only. **Do not push or open a PR** without asking first —
  confirmed explicitly with the user for this session.
- The `jobs`/`technicians`/`_meta` tab contract is frozen — nothing in this plan
  changes a column name or order.
- `technician_rating` has **no known ServiceTitan write endpoint** in this CLI
  or registry. Do not guess one. Implement it as an explicit, reported failure
  (`UnsupportedOutboxKindError`) and raise this as an open question in the final
  report — do not silently drop the item or invent an endpoint.
- The Outbox response envelope (`GET /crm-outbox`'s exact JSON shape beyond
  `{id, idempotency_key, kind, payload}` per item) and the claim `limit` default
  are not specified in the spec. Pick reasonable values, document them as
  assumptions in `KNOWN_UNVERIFIED.md`, and do not treat them as settled.

---

## File Structure

**New files (`ST-exporter`, this repo):**
- `src/st_exporter/traderated_settings.py` — `TradeRatedSettings` (optional,
  env-only outbox config).
- `src/st_exporter/outbox/__init__.py` — empty, package marker.
- `src/st_exporter/outbox/client.py` — `OutboxItem`, `TradeRatedOutboxClient`.
- `src/st_exporter/outbox/ledger.py` — `LedgerEntry`, `OutboxLedger`.
- `src/st_exporter/outbox/actions.py` — `UnsupportedOutboxKindError`,
  `perform_item`.
- `src/st_exporter/outbox/drain.py` — `DrainSummary`, `drain_outbox`.
- `.github/workflows/export.yml` — the reusable `workflow_call` workflow.
- `tests/st_exporter/test_feeds.py` — `parse_feeds` unit tests.
- `tests/st_exporter/test_traderated_settings.py`
- `tests/st_exporter/outbox/__init__.py`
- `tests/st_exporter/outbox/test_client.py`
- `tests/st_exporter/outbox/test_ledger.py`
- `tests/st_exporter/outbox/test_actions.py`
- `tests/st_exporter/outbox/test_drain.py`

**Modified files (`ST-exporter`):**
- `src/st_exporter/run.py` — add `parse_feeds`/`DEFAULT_FEEDS`, thread `feeds`
  through `run_export`/`_run` so a call can fetch/write just `jobs` or just
  `technicians`.
- `src/st_exporter/cli.py` — add `--feeds`, build `TradeRatedSettings`, drain
  the outbox after a non-dry-run export when configured.
- `src/st_exporter/__init__.py` — bump `EXPORTER_VERSION`.
- `tests/st_exporter/test_run_integration.py` — new feed-subset scenarios.
- `tests/st_exporter/test_cli.py` — update call-signature assertions, add
  outbox-drain wiring tests.
- `KNOWN_UNVERIFIED.md` — new entries for the outbox assumptions above.
- `pyproject.toml` — no dependency changes; version bump is `EXPORTER_VERSION`
  only (`st-cli`'s own `version` field is untouched — it tracks the CLI/MCP
  surface, not the exporter).

**New files (`BBTT-01/tr-doorservpro`, separate repo, cloned locally):**
- `.github/workflows/export.yml` — caller workflow, two schedules.
- `SETUP.md` — owner-facing setup guide.

---

### Task 1: `parse_feeds` and `DEFAULT_FEEDS`

**Files:**
- Modify: `src/st_exporter/run.py` (add near the top, after the `_RAW_*`
  constants at line 42)
- Test: `tests/st_exporter/test_feeds.py`

**Interfaces:**
- Produces: `st_exporter.run.DEFAULT_FEEDS: frozenset[str]` (`{"jobs",
  "technicians"}`), `st_exporter.run.parse_feeds(value: str) -> frozenset[str]`,
  raising `st_cli.exceptions.ConfigError` on an empty or unknown feed name.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for parse_feeds — the --feeds CLI option's validation."""

from __future__ import annotations

import pytest

from st_cli.exceptions import ConfigError
from st_exporter.run import DEFAULT_FEEDS, parse_feeds


class TestParseFeeds:
    def test_both_feeds(self) -> None:
        assert parse_feeds("jobs,technicians") == {"jobs", "technicians"}

    def test_single_feed(self) -> None:
        assert parse_feeds("jobs") == {"jobs"}

    def test_whitespace_and_trailing_comma_tolerated(self) -> None:
        assert parse_feeds(" jobs , technicians, ") == {"jobs", "technicians"}

    def test_default_feeds_constant_is_both(self) -> None:
        assert DEFAULT_FEEDS == {"jobs", "technicians"}

    def test_unknown_feed_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="unknown"):
            parse_feeds("jobs,pricebook")

    def test_empty_string_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="at least one"):
            parse_feeds("")

    def test_only_commas_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="at least one"):
            parse_feeds(" , ,")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/st_exporter/test_feeds.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_feeds' from 'st_exporter.run'`

- [ ] **Step 3: Implement**

In `src/st_exporter/run.py`, add after the existing `_RAW_ASSIGNMENTS = "_raw_assignments"`
constant (currently line 42) and before the `@dataclass class ExportSummary`:

```python
_VALID_FEEDS = frozenset({"jobs", "technicians"})
DEFAULT_FEEDS = frozenset({"jobs", "technicians"})


def parse_feeds(value: str) -> frozenset[str]:
    """Parse a comma-separated --feeds value into a validated set.

    Blank segments are dropped so "jobs," or " jobs , technicians " both work —
    the reusable workflow's `feeds` input is free-text, not a strict enum.
    """
    feeds = frozenset(part.strip() for part in value.split(",") if part.strip())
    if not feeds:
        raise ConfigError("--feeds must name at least one of: jobs, technicians.")
    unknown = feeds - _VALID_FEEDS
    if unknown:
        raise ConfigError(
            f"unknown feed(s): {', '.join(sorted(unknown))}. Valid feeds: jobs, technicians."
        )
    return feeds
```

Add `ConfigError` to the existing imports at the top of `run.py`. Today the file
has no import from `st_cli.exceptions` — add this line among the existing
`from st_cli...` imports (after `from st_cli.config import Settings`):

```python
from st_cli.exceptions import ConfigError
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/st_exporter/test_feeds.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/st_exporter/run.py tests/st_exporter/test_feeds.py
git commit -m "feat(exporter): add --feeds parsing (jobs/technicians subset selection)"
```

---

### Task 2: Thread `feeds` through `run_export`/`_run`

This is the biggest single change in the plan. Read all of it before editing —
the ordering of edits matters because `_run`'s body is being restructured
around two independently-gated blocks.

**Files:**
- Modify: `src/st_exporter/run.py:53-90` (`run_export`), `src/st_exporter/run.py:115-258`
  (`_run`)
- Test: `tests/st_exporter/test_run_integration.py` (new test functions
  appended)

**Interfaces:**
- Consumes: `parse_feeds`, `DEFAULT_FEEDS` from Task 1.
- Produces: `run_export(..., feeds: frozenset[str] = DEFAULT_FEEDS, ...)` —
  every existing caller that omits `feeds` keeps today's exact behavior (both
  feeds, same grids, same `_meta` rows, same `ExportSummary` fields). This is
  the contract `test_run_integration.py`'s two existing tests rely on; they
  must keep passing unmodified.

- [ ] **Step 1: Write the failing tests**

Append to `tests/st_exporter/test_run_integration.py` (uses the same
`st_settings`/`exporter_settings` fixtures, `mock_auth_token`, and
`InMemorySheetsStore` already imported at the top of that file):

```python
@respx.mock
def test_jobs_only_run_does_not_touch_technicians_tab_or_raw_cache(
    st_settings, exporter_settings
) -> None:
    api_base = st_settings.api_base
    today_iso = FIXED_TODAY.isoformat()
    far_past_iso = (FIXED_TODAY - timedelta(days=200)).isoformat()

    export_store = InMemorySheetsStore()
    raw_cache_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)
    tenant_run1.register(api_base, today_iso=today_iso, far_past_iso=far_past_iso)

    with _frozen_now():
        summary = run_export(
            st_settings,
            exporter_settings,
            feeds=frozenset({"jobs"}),
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )

    assert summary.jobs_row_count == 1
    assert "jobs" in export_store.tabs
    assert "technicians" not in export_store.tabs
    assert raw_cache_store.tabs == {}, "a jobs-only run must not touch the raw-cache sheet at all"

    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert set(meta) == {"jobs"}


@respx.mock
def test_technicians_only_run_preserves_existing_jobs_tab_and_meta(
    st_settings, exporter_settings
) -> None:
    api_base = st_settings.api_base
    today_iso = FIXED_TODAY.isoformat()
    far_past_iso = (FIXED_TODAY - timedelta(days=200)).isoformat()

    export_store = InMemorySheetsStore()
    raw_cache_store = InMemorySheetsStore()
    mock_auth_token(st_settings.auth_url)

    # Run 1: a normal both-feeds run establishes a jobs tab and jobs _meta row.
    tenant_run1.register(api_base, today_iso=today_iso, far_past_iso=far_past_iso)
    with _frozen_now():
        run_export(
            st_settings,
            exporter_settings,
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )
    jobs_tab_after_run1 = export_store.tabs["jobs"]
    jobs_meta_after_run1 = parse_meta_grid(export_store.tabs["_meta"])["jobs"]

    # Run 2: technicians-only. No new ServiceTitan routes are registered for the
    # jobs-side feeds (customers/locations/jobs/appointments/assignments) — if
    # _run() tried to fetch any of them, respx would raise for the unmocked call,
    # which is the proof this run touches nothing on the jobs side.
    respx.get(f"{api_base}/settings/v2/tenant/{st_settings.tenant_id}/technicians").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": 9, "name": "New Tech", "email": "nt@example.com", "active": True}],
                "hasMore": False,
                "continueFrom": None,
            },
        )
    )
    with _frozen_now():
        summary = run_export(
            st_settings,
            exporter_settings,
            feeds=frozenset({"technicians"}),
            export_store=export_store,
            raw_cache_store=raw_cache_store,
        )

    assert summary.technicians_row_count == 1
    assert summary.jobs_row_count == jobs_meta_after_run1.row_count
    assert export_store.tabs["jobs"] == jobs_tab_after_run1, "untouched feed's tab must survive"

    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert meta["jobs"] == jobs_meta_after_run1, "untouched feed's _meta row must be carried forward"
    assert meta["technicians"].row_count == 1
```

Check `tests/st_exporter/fixtures/tenant_run1.py`'s `register()` for the exact
technicians route it registers, and confirm the URL/response shape used above
matches `fetch_technicians`'s expectations in
`src/st_exporter/feeds/reference.py` before running — copy the route pattern
from there rather than guessing if it differs.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/st_exporter/test_run_integration.py -k "jobs_only or technicians_only" -v`
Expected: FAIL — `TypeError: run_export() got an unexpected keyword argument 'feeds'`

- [ ] **Step 3: Implement**

Replace `run_export`'s signature and body (currently `src/st_exporter/run.py:53-90`)
with:

```python
def run_export(
    st_settings: Settings,
    exporter_settings: ExporterSettings,
    *,
    feeds: frozenset[str] = DEFAULT_FEEDS,
    pricebook: bool = False,
    dry_run: bool = False,
    client: ServiceTitanClient | None = None,
    export_store: SheetsPort | None = None,
    raw_cache_store: SheetsPort | None = None,
) -> ExportSummary:
    """Run one export: fetch feeds, denormalise, window-filter, write the Sheets.

    ``feeds`` selects which output tab(s) this call fetches and writes — either
    subset lets the reusable workflow honor the spec's different cadences (jobs
    ~5 min, technicians ~30 min) without re-fetching the unneeded feed every
    time. The feed not selected is left byte-for-byte untouched in both the
    Export Store's tab and its `_meta` row.

    ``client``/``export_store``/``raw_cache_store`` can be injected (used by tests
    with fixtures and an in-memory Sheets double); left as ``None`` in production,
    where real ones are constructed from ``st_settings``/``exporter_settings``.
    ``pricebook`` is accepted and does nothing when set, per ticket 05's explicit
    scope cut — the CLI has no price-book commands to call anyway.
    """
    configure_logging()
    if pricebook:
        logger.info("pricebook=true has no effect (out of scope for this exporter)")

    owns_client = client is None
    active_client = client or ServiceTitanClient(st_settings)
    try:
        active_export_store, active_raw_cache_store = _resolve_stores(
            exporter_settings, export_store, raw_cache_store, dry_run=dry_run
        )
        return _run(
            active_client,
            active_export_store,
            active_raw_cache_store,
            feeds=feeds,
            window_days=exporter_settings.window_days,
            dry_run=dry_run,
        )
    finally:
        if owns_client:
            active_client.close()
```

Replace `_run` (currently `src/st_exporter/run.py:115-258`) in full with:

```python
def _run(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    raw_cache_store: SheetsPort,
    *,
    feeds: frozenset[str] = DEFAULT_FEEDS,
    window_days: int = DEFAULT_WINDOW_DAYS,
    dry_run: bool,
) -> ExportSummary:
    now = datetime.now(timezone.utc)
    today = now.date()
    run_at = now.isoformat()

    meta_rows = parse_meta_grid(export_store.read_grid("_meta"))
    new_meta_rows: list[MetaRow] = []

    windowed_rows: list[dict[str, Any]] = []
    skipped_no_job = 0
    jobs_row_count = meta_rows["jobs"].row_count if "jobs" in meta_rows else 0

    if "jobs" in feeds:
        windowed_rows, skipped_no_job = _run_jobs_feed(
            client,
            export_store,
            raw_cache_store,
            meta_rows=meta_rows,
            new_meta_rows=new_meta_rows,
            today=today,
            run_at=run_at,
            window_days=window_days,
            dry_run=dry_run,
        )
        jobs_row_count = len(windowed_rows)
    elif "jobs" in meta_rows:
        new_meta_rows.append(meta_rows["jobs"])

    technician_rows: list[dict[str, Any]] = []
    technicians_row_count = (
        meta_rows["technicians"].row_count if "technicians" in meta_rows else 0
    )

    if "technicians" in feeds:
        technician_rows = _run_technicians_feed(
            client,
            export_store,
            new_meta_rows=new_meta_rows,
            run_at=run_at,
            dry_run=dry_run,
        )
        technicians_row_count = len(technician_rows)
    elif "technicians" in meta_rows:
        new_meta_rows.append(meta_rows["technicians"])

    if dry_run:
        logger.info(
            "dry-run: would write jobs=%d technicians=%d (nothing written)",
            jobs_row_count,
            technicians_row_count,
        )
    else:
        export_store.replace_grid("_meta", build_meta_grid(new_meta_rows))

    return ExportSummary(
        jobs_row_count=jobs_row_count,
        technicians_row_count=technicians_row_count,
        skipped_no_job=skipped_no_job,
        dry_run=dry_run,
    )


def _run_jobs_feed(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    raw_cache_store: SheetsPort,
    *,
    meta_rows: dict[str, MetaRow],
    new_meta_rows: list[MetaRow],
    today: Any,
    run_at: str,
    window_days: int,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch/denormalise/window the jobs feed; return (windowed_rows, skipped_no_job).

    Mutates ``new_meta_rows`` in place (appends the jobs MetaRow) so the caller
    doesn't need a second merge step — mirrors how the technicians half works.
    """
    jobs_meta = meta_rows.get("jobs")
    cursor_bundle = CursorBundle.decode(jobs_meta.last_cursor if jobs_meta else None)

    raw_customers = RawCache.from_grid(raw_cache_store.read_grid(_RAW_CUSTOMERS))
    raw_locations = RawCache.from_grid(raw_cache_store.read_grid(_RAW_LOCATIONS))
    raw_jobs = RawCache.from_grid(raw_cache_store.read_grid(_RAW_JOBS))
    raw_appointments = RawCache.from_grid(raw_cache_store.read_grid(_RAW_APPOINTMENTS))
    raw_assignments = RawCache.from_grid(raw_cache_store.read_grid(_RAW_ASSIGNMENTS))

    customers_delta, customers_cursor = fetch_customers_delta(
        client, cursor_bundle.get("customers")
    )
    locations_delta, locations_cursor = fetch_locations_delta(
        client, cursor_bundle.get("locations")
    )
    jobs_delta, jobs_cursor = fetch_jobs_delta(client, cursor_bundle.get("jobs"))
    appointments_delta, appointments_cursor = fetch_appointments_delta(
        client, cursor_bundle.get("appointments")
    )
    assignments_delta, assignments_cursor = fetch_assignments_delta(
        client, cursor_bundle.get("assignments")
    )

    raw_customers.merge(customers_delta)
    raw_locations.merge(locations_delta)
    raw_jobs.merge(jobs_delta)
    raw_appointments.merge(appointments_delta)
    raw_assignments.merge(assignments_delta)

    logger.info(
        "fetched deltas: customers=%d locations=%d jobs=%d appointments=%d assignments=%d",
        len(customers_delta),
        len(locations_delta),
        len(jobs_delta),
        len(appointments_delta),
        len(assignments_delta),
    )

    job_types = fetch_job_types(client)
    business_units = fetch_business_units(client)

    denormalized = build_job_rows(
        raw_jobs,
        raw_appointments,
        raw_assignments,
        raw_customers,
        raw_locations,
        job_types,
        business_units,
    )
    if denormalized.skipped_no_job:
        logger.info(
            "skipped %d appointment(s) whose job hasn't arrived in the raw cache yet",
            denormalized.skipped_no_job,
        )

    windowed_rows, skipped_no_start, skipped_bad_timestamp = _apply_window(
        denormalized.rows, today=today, window_days=window_days
    )
    if skipped_no_start:
        logger.info("skipped %d appointment(s) with no appointment_start at all", skipped_no_start)
    if skipped_bad_timestamp:
        logger.info(
            "skipped %d appointment(s) with an unparsable appointment_start "
            "(excluded rather than crashing the run)",
            skipped_bad_timestamp,
        )

    jobs_grid = [list(JOB_COLUMNS)] + [format_job_row(row) for row in windowed_rows]

    new_cursor_bundle = CursorBundle(
        {
            "customers": customers_cursor,
            "locations": locations_cursor,
            "jobs": jobs_cursor,
            "appointments": appointments_cursor,
            "assignments": assignments_cursor,
        }
    )
    new_meta_rows.append(
        MetaRow(
            feed="jobs",
            last_run_at=run_at,
            last_cursor=new_cursor_bundle.encode(),
            row_count=len(windowed_rows),
            exporter_version=EXPORTER_VERSION,
        )
    )

    if not dry_run:
        _prune_raw_caches(
            denormalized.rows,
            windowed_rows,
            raw_appointments=raw_appointments,
            raw_assignments=raw_assignments,
        )
        raw_cache_store.replace_grid(_RAW_CUSTOMERS, raw_customers.to_grid())
        raw_cache_store.replace_grid(_RAW_LOCATIONS, raw_locations.to_grid())
        raw_cache_store.replace_grid(_RAW_JOBS, raw_jobs.to_grid())
        raw_cache_store.replace_grid(_RAW_APPOINTMENTS, raw_appointments.to_grid())
        raw_cache_store.replace_grid(_RAW_ASSIGNMENTS, raw_assignments.to_grid())
        export_store.replace_grid("jobs", jobs_grid)

    return windowed_rows, denormalized.skipped_no_job


def _run_technicians_feed(
    client: ServiceTitanClient,
    export_store: SheetsPort,
    *,
    new_meta_rows: list[MetaRow],
    run_at: str,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Fetch technicians and (unless dry-run) write the tab; append its MetaRow."""
    technicians = fetch_technicians(client)
    technician_rows = [_technician_row(record) for record in technicians]
    new_meta_rows.append(
        MetaRow(
            feed="technicians",
            last_run_at=run_at,
            last_cursor="",
            row_count=len(technician_rows),
            exporter_version=EXPORTER_VERSION,
        )
    )
    if not dry_run:
        technicians_grid = [list(TECHNICIAN_COLUMNS)] + [
            format_technician_row(row) for row in technician_rows
        ]
        export_store.replace_grid("technicians", technicians_grid)
    return technician_rows
```

`_technician_row`, `_apply_window`, `_is_parseable_timestamp`, and
`_prune_raw_caches` (currently `run.py:261-355`) are unchanged — leave them
exactly as they are, below the new functions.

Note what this refactor deliberately preserves: when `feeds=DEFAULT_FEEDS`
(the default), `_run_jobs_feed` and `_run_technicians_feed` both execute in the
same order as the original code, `new_meta_rows` ends up with the same two
`MetaRow`s, and `build_meta_grid` sorts by feed name regardless of append
order — so the two existing tests in `test_run_integration.py` must pass with
**zero changes** to their assertions.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/st_exporter/test_run_integration.py -v`
Expected: all tests pass, including the two pre-existing ones (unmodified) and
the two new ones from Step 1.

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `pytest -q`
Expected: same pass/fail counts as before this task except the newly added
tests now passing (5 pre-existing unrelated failures in
`tests/commands/test_reporting.py`/`tests/test_output.py` are expected — see
this plan's final verification task for why those are out of scope here).

- [ ] **Step 6: Commit**

```bash
git add src/st_exporter/run.py tests/st_exporter/test_run_integration.py
git commit -m "feat(exporter): support running a single feed (jobs or technicians) per call"
```

---

### Task 3: Wire `--feeds` into the `st-export` CLI

**Files:**
- Modify: `src/st_exporter/cli.py` (full file, currently 53 lines)
- Test: `tests/st_exporter/test_cli.py` (update existing assertions, add new
  tests)

**Interfaces:**
- Consumes: `parse_feeds`, `DEFAULT_FEEDS` from Task 1/2.
- Produces: `st-export --feeds jobs`, `st-export --feeds technicians`,
  `st-export` (defaults to both) — matches the reusable workflow's `feeds`
  input from Task on the workflow YAML.

- [ ] **Step 1: Write the failing tests**

In `tests/st_exporter/test_cli.py`, update every existing
`mock_run.assert_called_once_with(...)` call to include `feeds=DEFAULT_FEEDS`.
For example, the first one in `TestSuccessPath.test_echoes_summary_and_exits_zero`
becomes:

```python
        mock_run.assert_called_once_with(
            "fake-st-settings",
            "fake-exporter-settings",
            feeds=DEFAULT_FEEDS,
            pricebook=False,
            dry_run=False,
        )
```

Apply the same `feeds=DEFAULT_FEEDS` addition to the other two
`assert_called_once_with` calls in `TestSuccessPath`
(`test_dry_run_flag_is_passed_through`, `test_pricebook_flag_is_passed_through`).
Add the import at the top of the file: `from st_exporter.run import DEFAULT_FEEDS`.

Then add a new test class in the same file:

```python
class TestFeedsFlag:
    def test_feeds_flag_is_parsed_and_passed_through(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "jobs"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.run_export", return_value=_summary()) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_run.assert_called_once_with(
            "s", "e", feeds=frozenset({"jobs"}), pricebook=False, dry_run=False
        )

    def test_invalid_feeds_value_prints_clean_error_and_exits_one(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--feeds", "not-a-feed"])
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        assert "Error:" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/st_exporter/test_cli.py -v`
Expected: FAIL — existing assertions fail (`feeds` kwarg missing from the actual
call) and `TestFeedsFlag` fails with `NoSuchOption: --feeds`.

- [ ] **Step 3: Implement**

Replace `src/st_exporter/cli.py` in full:

```python
"""``st-export`` — the exporter's CLI entrypoint (one command, run by GitHub Actions)."""

from __future__ import annotations

import typer
from pydantic import ValidationError

from st_cli.config import load_settings
from st_cli.exceptions import STCLIError
from st_exporter.config import ExporterSettings
from st_exporter.run import DEFAULT_FEEDS, parse_feeds, run_export


def run_once(
    feeds: str = typer.Option(
        "jobs,technicians",
        "--feeds",
        help="Comma-separated feeds to run this call: jobs, technicians, or both.",
    ),
    pricebook: bool = typer.Option(
        False,
        help="No-op; reserved for a future price-book feed the CLI doesn't support yet.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compute the run but don't write to Google Sheets."
    ),
) -> None:
    """Run one ServiceTitan -> Export Store export pass."""
    st_settings = load_settings()
    exporter_settings = ExporterSettings()  # type: ignore[call-arg]
    summary = run_export(
        st_settings,
        exporter_settings,
        feeds=parse_feeds(feeds),
        pricebook=pricebook,
        dry_run=dry_run,
    )
    typer.echo(
        f"jobs={summary.jobs_row_count} technicians={summary.technicians_row_count} "
        f"skipped_no_job={summary.skipped_no_job} dry_run={summary.dry_run}"
    )


def main() -> None:
    """Entry point for the `st-export` command."""
    try:
        typer.run(run_once)
    except (STCLIError, ValidationError) as exc:
        # ValidationError covers a missing/malformed env var surfacing from
        # load_settings()/ExporterSettings() — those aren't STCLIError subclasses,
        # so without this a bad Actions secret prints a raw pydantic traceback
        # instead of the same clean Error: ... + exit-1 path. parse_feeds() raising
        # ConfigError (an STCLIError) on a bad --feeds value takes the same path.
        #
        # Deliberately SystemExit, not typer.Exit: this except block runs after
        # typer.run(run_once) has already returned control to us, outside any
        # Click/Typer dispatch loop, so nothing would translate a typer.Exit into
        # an actual clean process exit here — it would just be an uncaught
        # exception (Python prints a traceback and exits 1 anyway, but with a
        # traceback dumped on top of the "Error: ..." line, defeating the point).
        # SystemExit is what Python's interpreter itself treats specially: a
        # clean exit with no traceback.
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc
```

Note `DEFAULT_FEEDS` is imported but only used by the test file — `ruff` will
flag an unused import in `cli.py` if it isn't actually referenced there. It
isn't needed in `cli.py`'s own code (the typer default is the literal string
`"jobs,technicians"`, parsed the same way a real invocation would be), so
**do not import `DEFAULT_FEEDS` in `cli.py`** — only in the test file. This
also means `parse_feeds("jobs,technicians")` runs on every default invocation,
which is intentional: it's the same validation path a customer's `--feeds`
override takes, so there's only one code path to trust.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/st_exporter/test_cli.py -v`
Expected: all pass.

- [ ] **Step 5: Lint check**

Run: `ruff check src/st_exporter/cli.py tests/st_exporter/test_cli.py`
Expected: no errors (in particular, no unused-import warning on `cli.py`).

- [ ] **Step 6: Commit**

```bash
git add src/st_exporter/cli.py tests/st_exporter/test_cli.py
git commit -m "feat(exporter): expose --feeds on the st-export CLI"
```

---

### Task 4: `TradeRatedSettings`

**Files:**
- Create: `src/st_exporter/traderated_settings.py`
- Test: `tests/st_exporter/test_traderated_settings.py`

**Interfaces:**
- Produces: `TradeRatedSettings` (pydantic `BaseSettings`, `env_prefix=
  "TRADERATED_"`) with `machine_token: str | None`, `outbox_base_url: str |
  None`, and a `configured` property — `True` only when both are set. Consumed
  by Task 9 (`cli.py`'s outbox wiring) and Task 8 (`drain.py`'s tests).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for TradeRatedSettings — optional-by-design outbox configuration."""

from __future__ import annotations

from st_exporter.traderated_settings import TradeRatedSettings


class TestTradeRatedSettings:
    def test_unset_is_not_configured(self, monkeypatch) -> None:
        monkeypatch.delenv("TRADERATED_MACHINE_TOKEN", raising=False)
        monkeypatch.delenv("TRADERATED_OUTBOX_BASE_URL", raising=False)
        settings = TradeRatedSettings()
        assert settings.configured is False

    def test_both_set_is_configured(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "tok")
        monkeypatch.setenv("TRADERATED_OUTBOX_BASE_URL", "https://example.com")
        settings = TradeRatedSettings()
        assert settings.configured is True

    def test_only_one_set_is_not_configured(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "tok")
        monkeypatch.delenv("TRADERATED_OUTBOX_BASE_URL", raising=False)
        settings = TradeRatedSettings()
        assert settings.configured is False

    def test_machine_token_not_in_repr(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADERATED_MACHINE_TOKEN", "super-secret-token")
        settings = TradeRatedSettings()
        assert "super-secret-token" not in repr(settings)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/st_exporter/test_traderated_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'st_exporter.traderated_settings'`

- [ ] **Step 3: Implement**

```python
"""TradeRated Outbox settings, loaded from environment only.

Mirrors ``st_exporter.config.ExporterSettings``'s no-``.env``-fallback rule — a
GitHub Actions runner has none, so there must be no code path that reads one.

Optional by design: ticket 06 must be buildable and mergeable before ticket 07
issues the machine token and outbox URL (the handoff brief: "Neither blocks
ticket 05... build... and swap the target when 07 lands"). ``cli.py`` skips the
outbox drain entirely, with a log line, when ``configured`` is ``False``.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradeRatedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADERATED_")

    machine_token: str | None = Field(default=None, repr=False)
    outbox_base_url: str | None = None

    @property
    def configured(self) -> bool:
        return self.machine_token is not None and self.outbox_base_url is not None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/st_exporter/test_traderated_settings.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/st_exporter/traderated_settings.py tests/st_exporter/test_traderated_settings.py
git commit -m "feat(exporter): add optional TradeRatedSettings for the outbox drain"
```

---

### Task 5: `TradeRatedOutboxClient`

**Files:**
- Create: `src/st_exporter/outbox/__init__.py` (empty)
- Create: `src/st_exporter/outbox/client.py`
- Test: `tests/st_exporter/outbox/__init__.py` (empty)
- Test: `tests/st_exporter/outbox/test_client.py`

**Interfaces:**
- Produces: `OutboxItem` (frozen dataclass: `id: str`, `idempotency_key: str`,
  `kind: str`, `payload: dict[str, Any]`), `TradeRatedOutboxClient(base_url:
  str, machine_token: str)` with `.claim(limit: int = 10) -> list[OutboxItem]`,
  `.report_result(item_id: str, *, status: Literal["succeeded", "failed"],
  st_id: str | None = None, error: str | None = None) -> None`, `.close() ->
  None`. Consumed by Task 8 (`drain.py`).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for TradeRatedOutboxClient — the claim/report HTTP calls.

The exact response envelope (`GET /crm-outbox`'s shape beyond the per-item
{id, idempotency_key, kind, payload} the spec names) is not settled — see
KNOWN_UNVERIFIED.md. These tests fix the assumption this client makes
(`{"items": [...]}`) so a future correction is a one-place diff.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from st_exporter.outbox.client import OutboxItem, TradeRatedOutboxClient

BASE_URL = "https://outbox.example.com"


@pytest.fixture()
def client():
    c = TradeRatedOutboxClient(BASE_URL, "test-machine-token")
    yield c
    c.close()


class TestClaim:
    @respx.mock
    def test_claim_returns_parsed_items(self, client) -> None:
        respx.get(f"{BASE_URL}/crm-outbox").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "1",
                            "idempotency_key": "key-1",
                            "kind": "referral_lead",
                            "payload": {"name": "Jane"},
                        }
                    ]
                },
            )
        )
        items = client.claim(limit=5)
        assert items == [
            OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={"name": "Jane"})
        ]

    @respx.mock
    def test_claim_sends_bearer_token_and_limit(self, client) -> None:
        route = respx.get(f"{BASE_URL}/crm-outbox").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        client.claim(limit=7)
        request = route.calls.last.request
        assert request.headers["Authorization"] == "Bearer test-machine-token"
        assert request.url.params["limit"] == "7"

    @respx.mock
    def test_claim_empty_items_defaults_to_empty_list(self, client) -> None:
        respx.get(f"{BASE_URL}/crm-outbox").mock(return_value=httpx.Response(200, json={}))
        assert client.claim() == []

    @respx.mock
    def test_claim_raises_on_http_error(self, client) -> None:
        respx.get(f"{BASE_URL}/crm-outbox").mock(return_value=httpx.Response(401, text="nope"))
        with pytest.raises(httpx.HTTPStatusError):
            client.claim()


class TestReportResult:
    @respx.mock
    def test_report_success_includes_st_id(self, client) -> None:
        route = respx.post(f"{BASE_URL}/crm-outbox/1/result").mock(
            return_value=httpx.Response(200, json={})
        )
        client.report_result("1", status="succeeded", st_id="st-123")
        body = route.calls.last.request.content
        assert json.loads(body) == {"status": "succeeded", "st_id": "st-123"}

    @respx.mock
    def test_report_failure_includes_error(self, client) -> None:
        route = respx.post(f"{BASE_URL}/crm-outbox/1/result").mock(
            return_value=httpx.Response(200, json={})
        )
        client.report_result("1", status="failed", error="boom")
        body = json.loads(route.calls.last.request.content)
        assert body == {"status": "failed", "error": "boom"}

    @respx.mock
    def test_report_raises_on_http_error(self, client) -> None:
        respx.post(f"{BASE_URL}/crm-outbox/1/result").mock(
            return_value=httpx.Response(500, text="server error")
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.report_result("1", status="succeeded")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/st_exporter/outbox/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'st_exporter.outbox'`

- [ ] **Step 3: Implement**

`src/st_exporter/outbox/__init__.py`:

```python
"""TradeRated CRM Outbox drain — the return lane (spec.md, "Outbox contract")."""
```

`src/st_exporter/outbox/client.py`:

```python
"""HTTP client for TradeRated's CRM Outbox — the return lane.

Bearer-authenticated with the per-Company Machine Token (spec.md's "Machine
Token" section); the Company is resolved server-side from the token, never sent
by us. Two calls: claim pending items, report what happened to one.

The exact JSON envelope `GET /crm-outbox` wraps its items in is not specified
in the spec beyond the per-item shape — `{"items": [...]}` is this client's
assumption, flagged in KNOWN_UNVERIFIED.md. If TradeRated's real response
differs, this is the one place to fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

_DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class OutboxItem:
    id: str
    idempotency_key: str
    kind: str
    payload: dict[str, Any]


class TradeRatedOutboxClient:
    """Wraps ``GET {base}/crm-outbox`` and ``POST {base}/crm-outbox/{id}/result``."""

    def __init__(self, base_url: str, machine_token: str) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=_DEFAULT_TIMEOUT,
            headers={"Authorization": f"Bearer {machine_token}"},
        )

    def close(self) -> None:
        self._http.close()

    def claim(self, limit: int = 10) -> list[OutboxItem]:
        resp = self._http.get("/crm-outbox", params={"limit": limit})
        resp.raise_for_status()
        items = resp.json().get("items") or []
        return [
            OutboxItem(
                id=str(item["id"]),
                idempotency_key=item["idempotency_key"],
                kind=item["kind"],
                payload=item.get("payload") or {},
            )
            for item in items
        ]

    def report_result(
        self,
        item_id: str,
        *,
        status: Literal["succeeded", "failed"],
        st_id: str | None = None,
        error: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"status": status}
        if st_id is not None:
            body["st_id"] = st_id
        if error is not None:
            body["error"] = error
        resp = self._http.post(f"/crm-outbox/{item_id}/result", json=body)
        resp.raise_for_status()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/st_exporter/outbox/test_client.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/st_exporter/outbox/__init__.py src/st_exporter/outbox/client.py \
        tests/st_exporter/outbox/__init__.py tests/st_exporter/outbox/test_client.py
git commit -m "feat(exporter): add TradeRatedOutboxClient (claim/report HTTP calls)"
```

---

### Task 6: `OutboxLedger` — crash-safe idempotency

**Files:**
- Create: `src/st_exporter/outbox/ledger.py`
- Test: `tests/st_exporter/outbox/test_ledger.py`

**Interfaces:**
- Consumes: `SheetsPort` from `st_exporter.sheets` (already defined — `Task 5`
  of ticket 05, not this plan).
- Produces: `LedgerEntry` (frozen dataclass: `idempotency_key: str`, `kind:
  str`, `st_id: str`, `performed_at: str`), `OutboxLedger(store: SheetsPort)`
  with `.get(idempotency_key: str) -> LedgerEntry | None`, `.record(entry:
  LedgerEntry) -> None`, `.flush() -> None`. Consumed by Task 8 (`drain.py`).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for OutboxLedger — the _outbox_ledger tab that makes a redelivered
outbox item safe to re-report without re-performing the ServiceTitan write."""

from __future__ import annotations

from st_exporter.outbox.ledger import LedgerEntry, OutboxLedger
from st_exporter.sheets import InMemorySheetsStore


class TestOutboxLedger:
    def test_get_on_empty_store_returns_none(self) -> None:
        ledger = OutboxLedger(InMemorySheetsStore())
        assert ledger.get("missing-key") is None

    def test_record_then_get_round_trips(self) -> None:
        ledger = OutboxLedger(InMemorySheetsStore())
        entry = LedgerEntry(
            idempotency_key="key-1", kind="referral_lead", st_id="st-1", performed_at="2026-09-04T00:00:00+00:00"
        )
        ledger.record(entry)
        assert ledger.get("key-1") == entry

    def test_flush_writes_grid_and_survives_reload(self) -> None:
        store = InMemorySheetsStore()
        ledger = OutboxLedger(store)
        entry = LedgerEntry(
            idempotency_key="key-1", kind="referral_lead", st_id="st-1", performed_at="2026-09-04T00:00:00+00:00"
        )
        ledger.record(entry)
        ledger.flush()

        reloaded = OutboxLedger(store)
        assert reloaded.get("key-1") == entry

    def test_flush_without_any_get_or_record_is_a_noop(self) -> None:
        store = InMemorySheetsStore()
        ledger = OutboxLedger(store)
        ledger.flush()
        assert store.tabs == {}

    def test_malformed_row_is_skipped_not_crashed_on(self) -> None:
        store = InMemorySheetsStore()
        store.replace_grid(
            "_outbox_ledger",
            [["idempotency_key", "kind", "st_id", "performed_at"], ["too-short-row"]],
        )
        ledger = OutboxLedger(store)
        assert ledger.get("too-short-row") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/st_exporter/outbox/test_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'st_exporter.outbox.ledger'`

- [ ] **Step 3: Implement**

```python
"""Crash-safe idempotency ledger for the Outbox drain.

TradeRated's claim endpoint delivers at-least-once (spec.md's "Outbox contract"):
a run that crashes after writing to ServiceTitan but before reporting the result
will see the same item claimed again next run. This ledger — a tab on the
private raw-cache Sheet, never the shared Export Store — records every
idempotency_key this exporter has already performed, so a redelivered item is
recognised and only re-reported, never re-written to ServiceTitan. This is what
satisfies the spec's "yours needs to be safe to run twice" requirement; it does
not depend on ServiceTitan itself having any idempotency concept, because it
doesn't.
"""

from __future__ import annotations

from dataclasses import dataclass

from st_exporter.sheets import SheetsPort

_TAB_NAME = "_outbox_ledger"
_COLUMNS = ("idempotency_key", "kind", "st_id", "performed_at")


@dataclass(frozen=True)
class LedgerEntry:
    idempotency_key: str
    kind: str
    st_id: str
    performed_at: str


class OutboxLedger:
    """Read-modify-write wrapper around the `_outbox_ledger` tab.

    Loads lazily on first `.get()`/`.record()` (not in `__init__`) so
    constructing one never issues a Sheets read by itself; `.flush()` is a
    no-op until something actually touched the ledger, so a drain that claims
    zero items makes zero Sheets calls.
    """

    def __init__(self, store: SheetsPort) -> None:
        self._store = store
        self._entries: dict[str, LedgerEntry] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        grid = self._store.read_grid(_TAB_NAME)
        for row in grid[1:]:  # skip header
            if len(row) < len(_COLUMNS):
                continue  # malformed row; never crash a run over ledger corruption
            entry = LedgerEntry(*row[: len(_COLUMNS)])
            self._entries[entry.idempotency_key] = entry
        self._loaded = True

    def get(self, idempotency_key: str) -> LedgerEntry | None:
        self._ensure_loaded()
        return self._entries.get(idempotency_key)

    def record(self, entry: LedgerEntry) -> None:
        self._ensure_loaded()
        self._entries[entry.idempotency_key] = entry

    def flush(self) -> None:
        """Write the full ledger back in one call. No-op if nothing was loaded."""
        if not self._loaded:
            return
        grid: list[list[str]] = [list(_COLUMNS)]
        grid.extend(
            [e.idempotency_key, e.kind, e.st_id, e.performed_at] for e in self._entries.values()
        )
        self._store.replace_grid(_TAB_NAME, grid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/st_exporter/outbox/test_ledger.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/st_exporter/outbox/ledger.py tests/st_exporter/outbox/test_ledger.py
git commit -m "feat(exporter): add OutboxLedger for crash-safe outbox idempotency"
```

---

### Task 7: Outbox action dispatch (`referral_lead` / `technician_rating`)

**Files:**
- Create: `src/st_exporter/outbox/actions.py`
- Test: `tests/st_exporter/outbox/test_actions.py`

**Interfaces:**
- Consumes: `OutboxItem` from Task 5, `ServiceTitanClient` from
  `st_cli.client` (existing).
- Produces: `UnsupportedOutboxKindError(Exception)`, `perform_item(client:
  ServiceTitanClient, item: OutboxItem) -> str` (returns the ServiceTitan id).
  Consumed by Task 8 (`drain.py`).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for outbox action dispatch — one function per Outbox kind."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_exporter.outbox.actions import UnsupportedOutboxKindError, perform_item
from st_exporter.outbox.client import OutboxItem
from tests.st_exporter.conftest import mock_auth_token


class TestReferralLead:
    @respx.mock
    def test_creates_a_crm_lead_and_returns_its_id(self, st_settings) -> None:
        mock_auth_token(st_settings.auth_url)
        route = respx.post(
            f"{st_settings.api_base}/crm/v2/tenant/{st_settings.tenant_id}/leads"
        ).mock(return_value=httpx.Response(200, json={"id": 999}))

        client = ServiceTitanClient(st_settings)
        try:
            item = OutboxItem(
                id="1", idempotency_key="key-1", kind="referral_lead", payload={"name": "Jane"}
            )
            st_id = perform_item(client, item)
        finally:
            client.close()

        assert st_id == "999"
        assert json.loads(route.calls.last.request.content) == {"name": "Jane"}


class TestTechnicianRating:
    def test_raises_unsupported_kind(self, st_settings) -> None:
        client = ServiceTitanClient(st_settings)
        try:
            item = OutboxItem(
                id="2", idempotency_key="key-2", kind="technician_rating", payload={}
            )
            with pytest.raises(UnsupportedOutboxKindError, match="technician_rating"):
                perform_item(client, item)
        finally:
            client.close()


class TestUnknownKind:
    def test_raises_unsupported_kind(self, st_settings) -> None:
        client = ServiceTitanClient(st_settings)
        try:
            item = OutboxItem(id="3", idempotency_key="key-3", kind="something_else", payload={})
            with pytest.raises(UnsupportedOutboxKindError, match="something_else"):
                perform_item(client, item)
        finally:
            client.close()
```

This file uses the `st_settings` fixture from `tests/st_exporter/conftest.py`
(already defined) — no new fixtures needed. Note the `technician_rating` and
`something_else` cases never make an HTTP call, so they don't need
`@respx.mock` or `mock_auth_token`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/st_exporter/outbox/test_actions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'st_exporter.outbox.actions'`

- [ ] **Step 3: Implement**

```python
"""Dispatch: perform one Outbox item against ServiceTitan.

Kinds come from TradeRated's Outbox contract (spec.md, "Outbox contract"):
`referral_lead` and `technician_rating`. Each performer returns the ServiceTitan
id created/affected, which is what gets reported back so TradeRated can link its
own record to a real ServiceTitan entity.

`technician_rating` has no known ServiceTitan write endpoint anywhere in this
CLI's registry — ServiceTitan has no obvious native "post a rating" concept.
Rather than guess (a note? a custom field? something else?), it's raised here as
an explicit, distinguishable failure so the drain loop reports it back to
TradeRated as failed (releasing any credit hold) instead of silently dropping it
or inventing a write nobody asked for. Flagged in this ticket's final report as
an open question for the spec owner.
"""

from __future__ import annotations

from st_cli.client import ServiceTitanClient
from st_exporter.outbox.client import OutboxItem


class UnsupportedOutboxKindError(Exception):
    """Raised for an Outbox kind this exporter has no ServiceTitan write for yet."""


def perform_item(client: ServiceTitanClient, item: OutboxItem) -> str:
    """Perform ``item`` against ServiceTitan; return the resulting ServiceTitan id.

    Raises ``UnsupportedOutboxKindError`` for a kind with no known implementation —
    callers must catch this and report a failure rather than let it crash the run
    (see ``drain.py``).
    """
    if item.kind == "referral_lead":
        return _perform_referral_lead(client, item)
    if item.kind == "technician_rating":
        raise UnsupportedOutboxKindError(
            "technician_rating has no known ServiceTitan write endpoint yet "
            "(no rating concept in this CLI's registry) — needs clarification "
            "from the spec owner, not a guess."
        )
    raise UnsupportedOutboxKindError(f"unknown outbox kind: {item.kind!r}")


def _perform_referral_lead(client: ServiceTitanClient, item: OutboxItem) -> str:
    """Create a CRM lead from ``item.payload``.

    The payload's shape is TradeRated's to define (issue 04, not this repo's
    scope) and is assumed to already match ServiceTitan's lead-creation body —
    passed through as-is rather than remapped field-by-field, since remapping
    unknown fields would be guessing at a contract this repo doesn't own.
    """
    created = client.post("crm", "leads", json_body=item.payload)
    return str(created["id"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/st_exporter/outbox/test_actions.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/st_exporter/outbox/actions.py tests/st_exporter/outbox/test_actions.py
git commit -m "feat(exporter): dispatch referral_lead outbox items; flag technician_rating gap"
```

---

### Task 8: Drain orchestration

**Files:**
- Create: `src/st_exporter/outbox/drain.py`
- Test: `tests/st_exporter/outbox/test_drain.py`

**Interfaces:**
- Consumes: `ServiceTitanClient`, `TradeRatedOutboxClient`/`OutboxItem` (Task
  5), `OutboxLedger`/`LedgerEntry` (Task 6), `perform_item`/
  `UnsupportedOutboxKindError` (Task 7).
- Produces: `DrainSummary` (dataclass: `claimed: int`, `succeeded: int`,
  `failed: int`, `replayed: int`), `drain_outbox(client: ServiceTitanClient,
  outbox_client: TradeRatedOutboxClient, ledger: OutboxLedger, *, limit: int =
  10) -> DrainSummary`. Consumed by Task 9 (`cli.py`).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for drain_outbox — the claim -> perform -> ledger -> report loop."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from st_exporter.outbox.actions import UnsupportedOutboxKindError
from st_exporter.outbox.client import OutboxItem
from st_exporter.outbox.drain import DrainSummary, drain_outbox
from st_exporter.outbox.ledger import OutboxLedger
from st_exporter.sheets import InMemorySheetsStore


def _outbox_client(items):
    client = MagicMock()
    client.claim.return_value = items
    return client


class TestDrainOutbox:
    def test_empty_claim_is_a_full_noop(self) -> None:
        outbox_client = _outbox_client([])
        ledger = OutboxLedger(InMemorySheetsStore())
        summary = drain_outbox(MagicMock(), outbox_client, ledger)
        assert summary == DrainSummary(claimed=0, succeeded=0, failed=0, replayed=0)
        outbox_client.report_result.assert_not_called()

    def test_successful_referral_lead_is_recorded_and_reported(self, monkeypatch) -> None:
        item = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        outbox_client = _outbox_client([item])
        monkeypatch.setattr(
            "st_exporter.outbox.drain.perform_item", lambda client, item: "st-999"
        )
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), outbox_client, ledger)

        assert summary == DrainSummary(claimed=1, succeeded=1, failed=0, replayed=0)
        outbox_client.report_result.assert_called_once_with(
            "1", status="succeeded", st_id="st-999"
        )
        assert ledger.get("key-1").st_id == "st-999"

    def test_unsupported_kind_is_reported_failed_not_raised(self, monkeypatch) -> None:
        item = OutboxItem(id="2", idempotency_key="key-2", kind="technician_rating", payload={})
        outbox_client = _outbox_client([item])

        def _raise(client, item):
            raise UnsupportedOutboxKindError("technician_rating not supported")

        monkeypatch.setattr("st_exporter.outbox.drain.perform_item", _raise)
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), outbox_client, ledger)

        assert summary == DrainSummary(claimed=1, succeeded=0, failed=1, replayed=0)
        outbox_client.report_result.assert_called_once_with(
            "2", status="failed", error="technician_rating not supported"
        )
        assert ledger.get("key-2") is None

    def test_one_bad_item_does_not_stop_the_rest_of_the_batch(self, monkeypatch) -> None:
        good = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        bad = OutboxItem(id="2", idempotency_key="key-2", kind="referral_lead", payload={})
        outbox_client = _outbox_client([bad, good])

        def _perform(client, item):
            if item.id == "2":
                raise RuntimeError("network blip")
            return "st-1"

        monkeypatch.setattr("st_exporter.outbox.drain.perform_item", _perform)
        ledger = OutboxLedger(InMemorySheetsStore())

        summary = drain_outbox(MagicMock(), outbox_client, ledger)

        assert summary == DrainSummary(claimed=2, succeeded=1, failed=1, replayed=0)

    def test_redelivered_item_is_replayed_not_reperformed(self, monkeypatch) -> None:
        """The at-least-once-delivery safety property this whole ledger exists for:
        an item already in the ledger from a prior (possibly crashed) run must be
        re-reported using its recorded st_id, without calling perform_item again."""
        item = OutboxItem(id="1", idempotency_key="key-1", kind="referral_lead", payload={})
        outbox_client = _outbox_client([item])
        store = InMemorySheetsStore()
        ledger = OutboxLedger(store)

        calls = []
        monkeypatch.setattr(
            "st_exporter.outbox.drain.perform_item",
            lambda client, item: calls.append(item.id) or "st-999",
        )
        drain_outbox(MagicMock(), outbox_client, ledger)
        assert calls == ["1"]

        # Simulate the next run: fresh ledger instance reloading the flushed tab.
        outbox_client_2 = _outbox_client([item])
        ledger_2 = OutboxLedger(store)
        summary = drain_outbox(MagicMock(), outbox_client_2, ledger_2)

        assert calls == ["1"], "perform_item must not be called again for a replayed item"
        assert summary == DrainSummary(claimed=1, succeeded=0, failed=0, replayed=1)
        outbox_client_2.report_result.assert_called_once_with(
            "1", status="succeeded", st_id="st-999"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/st_exporter/outbox/test_drain.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'st_exporter.outbox.drain'`

- [ ] **Step 3: Implement**

```python
"""Drains TradeRated's CRM Outbox in the same run as the jobs/technicians export.

At-least-once delivery (spec.md) means a redelivered item must not double-write
to ServiceTitan — ``ledger.py``'s ``_outbox_ledger`` tab is what makes a retry
after a mid-run crash safe: an item already recorded there is only re-reported,
never re-performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from st_cli.client import ServiceTitanClient
from st_exporter.logging_setup import logger
from st_exporter.outbox.actions import UnsupportedOutboxKindError, perform_item
from st_exporter.outbox.client import TradeRatedOutboxClient
from st_exporter.outbox.ledger import LedgerEntry, OutboxLedger

_DEFAULT_CLAIM_LIMIT = 10


@dataclass
class DrainSummary:
    claimed: int
    succeeded: int
    failed: int
    replayed: int  # already in the ledger; re-reported without a new ST write


def drain_outbox(
    client: ServiceTitanClient,
    outbox_client: TradeRatedOutboxClient,
    ledger: OutboxLedger,
    *,
    limit: int = _DEFAULT_CLAIM_LIMIT,
) -> DrainSummary:
    items = outbox_client.claim(limit=limit)
    succeeded = failed = replayed = 0

    for item in items:
        existing = ledger.get(item.idempotency_key)
        if existing is not None:
            replayed += 1
            outbox_client.report_result(item.id, status="succeeded", st_id=existing.st_id)
            logger.info("outbox item %s already performed (idempotency replay)", item.id)
            continue

        try:
            st_id = perform_item(client, item)
        except UnsupportedOutboxKindError as exc:
            failed += 1
            logger.warning("outbox item %s (%s) not performed: %s", item.id, item.kind, exc)
            outbox_client.report_result(item.id, status="failed", error=str(exc))
            continue
        except Exception as exc:  # one bad item must not kill the rest of the batch
            failed += 1
            logger.warning("outbox item %s (%s) failed: %s", item.id, item.kind, exc)
            outbox_client.report_result(item.id, status="failed", error=str(exc))
            continue

        ledger.record(
            LedgerEntry(
                idempotency_key=item.idempotency_key,
                kind=item.kind,
                st_id=st_id,
                performed_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        outbox_client.report_result(item.id, status="succeeded", st_id=st_id)
        succeeded += 1

    ledger.flush()
    logger.info(
        "outbox drain: claimed=%d succeeded=%d failed=%d replayed=%d",
        len(items),
        succeeded,
        failed,
        replayed,
    )
    return DrainSummary(claimed=len(items), succeeded=succeeded, failed=failed, replayed=replayed)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/st_exporter/outbox/test_drain.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/st_exporter/outbox/drain.py tests/st_exporter/outbox/test_drain.py
git commit -m "feat(exporter): add drain_outbox orchestration loop"
```

---

### Task 9: Wire outbox drain into `st-export`

**Files:**
- Modify: `src/st_exporter/cli.py` (from Task 3's version)
- Test: `tests/st_exporter/test_cli.py` (new test class)

**Interfaces:**
- Consumes: `TradeRatedSettings` (Task 4), `drain_outbox`/`DrainSummary` (Task
  8), `OutboxLedger` (Task 6), `TradeRatedOutboxClient` (Task 5),
  `get_gspread_client`/`SheetsClient` (existing, `st_exporter.sheets`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/st_exporter/test_cli.py`:

```python
class TestOutboxDrain:
    def test_drains_outbox_when_configured_and_not_dry_run(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0])
        fake_traderated_settings = MagicMock(configured=True)
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.TradeRatedSettings", return_value=fake_traderated_settings),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli._drain_outbox") as mock_drain,
            pytest.raises(SystemExit),
        ):
            main()
        mock_drain.assert_called_once_with("s", "e", fake_traderated_settings)

    def test_skips_outbox_drain_when_not_configured(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0])
        fake_traderated_settings = MagicMock(configured=False)
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.TradeRatedSettings", return_value=fake_traderated_settings),
            patch("st_exporter.cli.run_export", return_value=_summary()),
            patch("st_exporter.cli._drain_outbox") as mock_drain,
            pytest.raises(SystemExit),
        ):
            main()
        mock_drain.assert_not_called()

    def test_skips_outbox_drain_on_dry_run_even_if_configured(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", [_ARGV0, "--dry-run"])
        fake_traderated_settings = MagicMock(configured=True)
        with (
            patch("st_exporter.cli.load_settings", return_value="s"),
            patch("st_exporter.cli.ExporterSettings", return_value="e"),
            patch("st_exporter.cli.TradeRatedSettings", return_value=fake_traderated_settings),
            patch("st_exporter.cli.run_export", return_value=_summary(dry_run=True)),
            patch("st_exporter.cli._drain_outbox") as mock_drain,
            pytest.raises(SystemExit),
        ):
            main()
        mock_drain.assert_not_called()
```

Add `from unittest.mock import MagicMock, patch` to the file's existing
`from unittest.mock import patch` import line (change it to import both names).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/st_exporter/test_cli.py -v`
Expected: FAIL — `AttributeError`/`ImportError` referencing `TradeRatedSettings`
and `_drain_outbox`, not yet present in `cli.py`.

- [ ] **Step 3: Implement**

Replace `src/st_exporter/cli.py` in full:

```python
"""``st-export`` — the exporter's CLI entrypoint (one command, run by GitHub Actions)."""

from __future__ import annotations

import typer
from pydantic import ValidationError

from st_cli.client import ServiceTitanClient
from st_cli.config import Settings, load_settings
from st_cli.exceptions import STCLIError
from st_exporter.config import ExporterSettings
from st_exporter.logging_setup import logger
from st_exporter.outbox.client import TradeRatedOutboxClient
from st_exporter.outbox.drain import DrainSummary, drain_outbox
from st_exporter.outbox.ledger import OutboxLedger
from st_exporter.run import parse_feeds, run_export
from st_exporter.sheets import SheetsClient, get_gspread_client
from st_exporter.traderated_settings import TradeRatedSettings


def run_once(
    feeds: str = typer.Option(
        "jobs,technicians",
        "--feeds",
        help="Comma-separated feeds to run this call: jobs, technicians, or both.",
    ),
    pricebook: bool = typer.Option(
        False,
        help="No-op; reserved for a future price-book feed the CLI doesn't support yet.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compute the run but don't write to Google Sheets."
    ),
) -> None:
    """Run one ServiceTitan -> Export Store export pass, then drain the CRM Outbox."""
    st_settings = load_settings()
    exporter_settings = ExporterSettings()  # type: ignore[call-arg]
    traderated_settings = TradeRatedSettings()  # type: ignore[call-arg]

    summary = run_export(
        st_settings,
        exporter_settings,
        feeds=parse_feeds(feeds),
        pricebook=pricebook,
        dry_run=dry_run,
    )

    outbox_summary: DrainSummary | None = None
    if not dry_run and traderated_settings.configured:
        outbox_summary = _drain_outbox(st_settings, exporter_settings, traderated_settings)
    elif not traderated_settings.configured:
        # Expected until ticket 07 issues the machine token/outbox URL — not an
        # error, so this is INFO, not WARNING.
        logger.info("TRADERATED_MACHINE_TOKEN/OUTBOX_BASE_URL not set; skipping outbox drain")

    message = (
        f"jobs={summary.jobs_row_count} technicians={summary.technicians_row_count} "
        f"skipped_no_job={summary.skipped_no_job} dry_run={summary.dry_run}"
    )
    if outbox_summary is not None:
        message += (
            f" outbox_claimed={outbox_summary.claimed} outbox_succeeded={outbox_summary.succeeded} "
            f"outbox_failed={outbox_summary.failed} outbox_replayed={outbox_summary.replayed}"
        )
    typer.echo(message)


def _drain_outbox(
    st_settings: Settings,
    exporter_settings: ExporterSettings,
    traderated_settings: TradeRatedSettings,
) -> DrainSummary:
    assert traderated_settings.machine_token is not None
    assert traderated_settings.outbox_base_url is not None

    gc = get_gspread_client(exporter_settings.service_account_json)
    raw_cache_store = SheetsClient.open(gc, exporter_settings.raw_cache_sheet_id)
    ledger = OutboxLedger(raw_cache_store)

    client = ServiceTitanClient(st_settings)
    outbox_client = TradeRatedOutboxClient(
        traderated_settings.outbox_base_url, traderated_settings.machine_token
    )
    try:
        return drain_outbox(client, outbox_client, ledger)
    finally:
        client.close()
        outbox_client.close()


def main() -> None:
    """Entry point for the `st-export` command."""
    try:
        typer.run(run_once)
    except (STCLIError, ValidationError) as exc:
        # ValidationError covers a missing/malformed env var surfacing from
        # load_settings()/ExporterSettings() — those aren't STCLIError subclasses,
        # so without this a bad Actions secret prints a raw pydantic traceback
        # instead of the same clean Error: ... + exit-1 path. parse_feeds() raising
        # ConfigError (an STCLIError) on a bad --feeds value takes the same path.
        #
        # Deliberately SystemExit, not typer.Exit: this except block runs after
        # typer.run(run_once) has already returned control to us, outside any
        # Click/Typer dispatch loop, so nothing would translate a typer.Exit into
        # an actual clean process exit here — it would just be an uncaught
        # exception (Python prints a traceback and exits 1 anyway, but with a
        # traceback dumped on top of the "Error: ..." line, defeating the point).
        # SystemExit is what Python's interpreter itself treats specially: a
        # clean exit with no traceback.
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc
```

Note: the earlier `test_echoes_summary_and_exits_zero` etc. from Task 3 patch
`st_exporter.cli.ExporterSettings` but not `st_exporter.cli.TradeRatedSettings`
— add `patch("st_exporter.cli.TradeRatedSettings", return_value=MagicMock(configured=False))`
to those three `TestSuccessPath` tests' `with` blocks too (otherwise a real
`TradeRatedSettings()` is constructed, which is harmless — all fields are
optional — but then `traderated_settings.configured` is `False` anyway since no
env vars are set in the test environment, so this is only needed if your local
`.env`/shell happens to export `TRADERATED_*` — patch it explicitly so the test
is hermetic regardless of the environment it runs in).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/st_exporter/test_cli.py -v`
Expected: all pass.

- [ ] **Step 5: Run the full test suite, lint, and type-check**

Run: `pytest -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/`
Expected: all pass except the 5 pre-existing, unrelated `capsys`/Rich-color
failures noted in Task 2's Step 5 — confirm the failure count hasn't grown.

- [ ] **Step 6: Commit**

```bash
git add src/st_exporter/cli.py tests/st_exporter/test_cli.py
git commit -m "feat(exporter): drain the CRM Outbox in the same run as the export"
```

---

### Task 10: Document the new assumptions in `KNOWN_UNVERIFIED.md`

**Files:**
- Modify: `KNOWN_UNVERIFIED.md` (append)

- [ ] **Step 1: Append new entries**

Add to the end of `KNOWN_UNVERIFIED.md`:

```markdown
## CRM Outbox response envelope

`src/st_exporter/outbox/client.py`, `TradeRatedOutboxClient.claim`

Assumes `GET /crm-outbox` wraps its items as `{"items": [...]}`. The spec names
the per-item shape (`id`, `idempotency_key`, `kind`, `payload`) but not the
envelope around the list. If TradeRated's real response differs (e.g. a bare
array, or a different key), this is the one function to fix.

## CRM Outbox claim limit

`src/st_exporter/outbox/drain.py`, `_DEFAULT_CLAIM_LIMIT`

Defaults to 10 pending items per drain. The spec says "up to N pending items"
without naming N. Unverified against a real deployment; adjust once ticket 07's
real outbox endpoint is live and its actual behavior/limits are known.

## `technician_rating` has no known ServiceTitan write

`src/st_exporter/outbox/actions.py`, `perform_item`

The Outbox contract names two kinds — `referral_lead` and `technician_rating` —
but this CLI's registry has no ServiceTitan endpoint that resembles "post a
rating for a technician." `perform_item` raises `UnsupportedOutboxKindError` for
this kind rather than guessing (a job note? a custom field? something else?).
Every `technician_rating` item will be reported back to TradeRated as `failed`
until this is resolved with the spec owner — raised explicitly in this ticket's
report, not silently worked around.

## `referral_lead` payload passed through unmapped

`src/st_exporter/outbox/actions.py`, `_perform_referral_lead`

`item.payload` is sent as-is to `POST /crm/v2/tenant/{id}/leads` — this repo
doesn't own the payload's shape (that's TradeRated's issue 04), so it's assumed
to already match ServiceTitan's lead-creation body rather than remapped
field-by-field. Whether ServiceTitan's real Lead-creation endpoint accepts
exactly TradeRated's queued fields (and what a rejection looks like) is
unconfirmed until a real end-to-end run exists.
```

- [ ] **Step 2: Commit**

```bash
git add KNOWN_UNVERIFIED.md
git commit -m "docs: flag outbox response-envelope and technician_rating assumptions"
```

---

### Task 11: Reusable workflow (`ST-exporter`)

**Files:**
- Create: `.github/workflows/export.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: Export

on:
  workflow_call:
    inputs:
      feeds:
        description: "Comma-separated feeds to run: jobs, technicians, or both"
        type: string
        default: "jobs,technicians"
      window:
        description: "Job window in days (appointment_start within last N days or future)"
        type: number
        default: 90
      target:
        description: "Export target; only 'sheets' is supported today"
        type: string
        default: "sheets"
      pricebook:
        description: "Reserved; has no effect (the CLI has no price-book commands)"
        type: boolean
        default: false
    secrets:
      ST_CLIENT_ID:
        required: true
      ST_CLIENT_SECRET:
        required: true
      ST_APP_KEY:
        required: true
      ST_TENANT_ID:
        required: true
      GOOGLE_SERVICE_ACCOUNT_JSON:
        required: true
      GOOGLE_SHEET_ID:
        required: true
      GOOGLE_RAW_CACHE_SHEET_ID:
        required: true
      TRADERATED_MACHINE_TOKEN:
        required: false
      TRADERATED_OUTBOX_BASE_URL:
        required: false

jobs:
  export:
    runs-on: ubuntu-latest
    if: ${{ inputs.target == 'sheets' }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install
        run: pip install -e .

      - name: Run export
        env:
          ST_CLIENT_ID: ${{ secrets.ST_CLIENT_ID }}
          ST_CLIENT_SECRET: ${{ secrets.ST_CLIENT_SECRET }}
          ST_APP_KEY: ${{ secrets.ST_APP_KEY }}
          ST_TENANT_ID: ${{ secrets.ST_TENANT_ID }}
          GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
          GOOGLE_SHEET_ID: ${{ secrets.GOOGLE_SHEET_ID }}
          GOOGLE_RAW_CACHE_SHEET_ID: ${{ secrets.GOOGLE_RAW_CACHE_SHEET_ID }}
          EXPORTER_WINDOW_DAYS: ${{ inputs.window }}
          TRADERATED_MACHINE_TOKEN: ${{ secrets.TRADERATED_MACHINE_TOKEN }}
          TRADERATED_OUTBOX_BASE_URL: ${{ secrets.TRADERATED_OUTBOX_BASE_URL }}
        run: |
          st-export --feeds "${{ inputs.feeds }}" ${{ inputs.pricebook && '--pricebook' || '' }}
```

Notes for the implementer:
- `if: ${{ inputs.target == 'sheets' }}` makes `target` meaningful today (the
  only supported value) without hand-building a case statement for values that
  don't exist yet — matches "no scope creep" (Airtable/Supabase targets are
  explicitly out of scope, so there's nothing else to branch on).
- No secret is echoed, printed, or passed as a CLI argument anywhere in this
  file — all four ServiceTitan values, both Sheets values, and both TradeRated
  values go through `env:`, matching how `st_cli.config.Settings` and
  `st_exporter.config.ExporterSettings`/`TradeRatedSettings` already expect to
  read them (`ST_*`, `GOOGLE_*`, `TRADERATED_*` prefixes respectively).
- `TRADERATED_MACHINE_TOKEN`/`TRADERATED_OUTBOX_BASE_URL` are `required: false`
  at the reusable-workflow level — a caller that hasn't received them yet (i.e.
  before ticket 07 lands) can omit them from its own secrets, and `cli.py`'s
  `TradeRatedSettings().configured` check (Task 9) makes that a graceful
  no-op, not a crash.

- [ ] **Step 2: Validate YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/export.yml'))"`
Expected: no exception.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/export.yml
git commit -m "feat: add reusable workflow_call export workflow"
```

---

### Task 12: Version bump and local tag

**Files:**
- Modify: `src/st_exporter/__init__.py`

**Interfaces:**
- Produces: `EXPORTER_VERSION = "0.2.0"` — this is what lands in every `_meta`
  row's `exporter_version` column going forward (see `meta.py`/`run.py`, which
  already reference `EXPORTER_VERSION` — no other file needs to change).

- [ ] **Step 1: Bump the version**

In `src/st_exporter/__init__.py`, change:

```python
EXPORTER_VERSION = "0.1.0"
```

to:

```python
EXPORTER_VERSION = "0.2.0"
```

- [ ] **Step 2: Run the full suite once more**

Run: `pytest -q`
Expected: same pass/fail counts as Task 9 Step 5 (the version string isn't
asserted literally anywhere — confirm with `grep -rn '0\.1\.0' tests/` and fix
any test that hardcodes the old string if one turns up; none are expected based
on this plan's earlier reading of the test suite).

- [ ] **Step 3: Commit**

```bash
git add src/st_exporter/__init__.py
git commit -m "chore(exporter): bump EXPORTER_VERSION to 0.2.0 for ticket 06"
```

- [ ] **Step 4: Tag locally (do not push)**

```bash
git tag -a exporter-v0.2.0 -m "Ticket 06: reusable workflow, outbox drain, --feeds selection"
```

This tag is what `tr-doorservpro`'s caller workflow (Task 15) pins to via
`uses: BBTT-01/ST-exporter/.github/workflows/export.yml@exporter-v0.2.0`. Per
this plan's global constraints, do not push the branch or the tag — flag both
as pending user approval in the final report.

---

### Task 13: Full verification pass (`ST-exporter`)

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `pytest -q`
Expected: every test added in Tasks 1–9 passes; the 5 pre-existing failures in
`tests/commands/test_reporting.py`/`tests/test_output.py` (ANSI-color leaking
into `capsys` on this Python 3.14 environment — confirmed pre-existing and
unrelated to tickets 05/06 during the readiness check for this ticket) are the
only failures. If any other test fails, stop and fix it before proceeding —
do not add it to the "known failing" list.

- [ ] **Step 2: Lint and format**

Run: `ruff check src/ tests/`
Expected: no errors.

Run: `ruff format --check src/ tests/`
Expected: no diffs.

- [ ] **Step 3: Type-check**

Run: `mypy src/`
Expected: no errors. Pay particular attention to `src/st_exporter/outbox/*.py`
and `src/st_exporter/traderated_settings.py` — these are new files under
`mypy --strict` (per `pyproject.toml`'s `[tool.mypy]`) and haven't been checked
yet in any earlier step of this plan.

- [ ] **Step 4: Report findings**

If Steps 1–3 are clean, proceed to Task 14. If not, fix and re-run before
moving to the second repo — `tr-doorservpro`'s workflow depends on this repo's
tag being correct, so don't tag/reference a broken state.

---

### Task 14: Clone and branch `tr-doorservpro`

**Files:** none (repo setup)

- [ ] **Step 1: Clone the repo into a scratch location**

```bash
mkdir -p /Users/clay/.claude/jobs/44f9b7b9/tmp/tr-doorservpro-work
git clone https://github.com/BBTT-01/tr-doorservpro.git \
  /Users/clay/.claude/jobs/44f9b7b9/tmp/tr-doorservpro-work
cd /Users/clay/.claude/jobs/44f9b7b9/tmp/tr-doorservpro-work
git checkout -b feat/servicetitan-hosted
```

Confirm the clone landed on `main` with just the `README.md` before branching
(matches what the readiness check found earlier this session: the repo has one
file, no other branches).

- [ ] **Step 2: Confirm clean state**

Run: `git status`
Expected: `On branch feat/servicetitan-hosted`, working tree clean.

---

### Task 15: Caller workflow (`tr-doorservpro`)

**Files:**
- Create: `/Users/clay/.claude/jobs/44f9b7b9/tmp/tr-doorservpro-work/.github/workflows/export.yml`

- [ ] **Step 1: Write the caller workflow**

```yaml
name: ServiceTitan Export

on:
  schedule:
    # GitHub Actions cron has a five-minute floor and is best-effort — this
    # will sometimes run every ~12 minutes rather than every 5. Do not promise
    # 5 minutes in SETUP.md.
    - cron: "*/5 * * * *"   # jobs + appointments + outbox drain
    - cron: "*/30 * * * *"  # technicians
  workflow_dispatch: {}

jobs:
  jobs-feed:
    if: github.event.schedule == '*/5 * * * *' || github.event_name == 'workflow_dispatch'
    uses: BBTT-01/ST-exporter/.github/workflows/export.yml@exporter-v0.2.0
    with:
      feeds: "jobs"
    secrets:
      ST_CLIENT_ID: ${{ secrets.ST_CLIENT_ID }}
      ST_CLIENT_SECRET: ${{ secrets.ST_CLIENT_SECRET }}
      ST_APP_KEY: ${{ secrets.ST_APP_KEY }}
      ST_TENANT_ID: ${{ secrets.ST_TENANT_ID }}
      GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
      GOOGLE_SHEET_ID: ${{ secrets.GOOGLE_SHEET_ID }}
      GOOGLE_RAW_CACHE_SHEET_ID: ${{ secrets.GOOGLE_RAW_CACHE_SHEET_ID }}
      TRADERATED_MACHINE_TOKEN: ${{ secrets.TRADERATED_MACHINE_TOKEN }}
      TRADERATED_OUTBOX_BASE_URL: ${{ secrets.TRADERATED_OUTBOX_BASE_URL }}

  technicians-feed:
    if: github.event.schedule == '*/30 * * * *' || github.event_name == 'workflow_dispatch'
    uses: BBTT-01/ST-exporter/.github/workflows/export.yml@exporter-v0.2.0
    with:
      feeds: "technicians"
    secrets:
      ST_CLIENT_ID: ${{ secrets.ST_CLIENT_ID }}
      ST_CLIENT_SECRET: ${{ secrets.ST_CLIENT_SECRET }}
      ST_APP_KEY: ${{ secrets.ST_APP_KEY }}
      ST_TENANT_ID: ${{ secrets.ST_TENANT_ID }}
      GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
      GOOGLE_SHEET_ID: ${{ secrets.GOOGLE_SHEET_ID }}
      GOOGLE_RAW_CACHE_SHEET_ID: ${{ secrets.GOOGLE_RAW_CACHE_SHEET_ID }}
```

Notes for the implementer:
- The outbox drain rides along with the `jobs-feed` job (per the spec's cadence
  table: "outbox drain | same run as jobs") — it doesn't need its own schedule
  or job; `st-export`'s own `cli.py` (Task 9) already drains it whenever
  `TRADERATED_*` secrets are present, regardless of which `--feeds` value was
  passed. The `technicians-feed` job omits the `TRADERATED_*` secrets entirely
  since they're irrelevant to a technicians-only call (outbox drain runs on the
  jobs call).
- Two `schedule` cron entries in one workflow both fire the same
  `on.schedule` event with a different `.schedule` string, which is what the
  two jobs' `if:` conditions branch on — this is GitHub Actions' documented
  pattern for "one workflow file, multiple cron cadences, different jobs per
  cadence." `workflow_dispatch` is added so the owner (or you) can manually
  trigger either job on demand for testing, without waiting on a schedule.
- `@exporter-v0.2.0` must exist as a pushed tag in `BBTT-01/ST-exporter` before
  this workflow can actually resolve — it won't yet, since Task 12 only tagged
  locally. Flag this clearly in the final report.

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/export.yml'))"`
Expected: no exception. (Use whatever Python is on PATH in this clone's
directory — it doesn't need this repo's own venv, just a YAML parser.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/export.yml
git commit -m "feat: add ServiceTitan export caller workflow (jobs @5min, technicians @30min)"
```

---

### Task 16: `SETUP.md` (`tr-doorservpro`)

**Files:**
- Create: `/Users/clay/.claude/jobs/44f9b7b9/tmp/tr-doorservpro-work/SETUP.md`

- [ ] **Step 1: Write SETUP.md**

```markdown
# Setting up your ServiceTitan connection

This connects your ServiceTitan account to TradeRated so your technicians see
today's jobs on their phones. It takes about ten minutes, and you don't need
any technical experience — just access to your ServiceTitan account and this
GitHub repository.

**You'll do two things:**
1. Create an "app" inside your own ServiceTitan account (ServiceTitan calls
   this a Developer Portal app).
2. Copy four values from that app, plus a Google Sheets key we give you, into
   this repository's settings.

Nobody at TradeRated ever sees your ServiceTitan login or your app's keys. You
create them, you paste them, and only GitHub stores them from that point on.

## Before you start

- You'll need to be logged into **your own** ServiceTitan account with
  permission to create integrations (usually an admin or owner login).
- Have this repository open in one browser tab, and ServiceTitan's Developer
  Portal open in another.
- Budget about 10 minutes.

## Step 1 — Create your ServiceTitan app

1. Go to ServiceTitan's Developer Portal and sign in with your normal
   ServiceTitan login: `https://developer.servicetitan.io`
2. Click **Create App** (or **New Application** — the exact wording may vary
   by ServiceTitan's current portal version).
3. Give it any name you like — for example, "TradeRated Export."
4. When asked which data it can access, approve:
   - **Customers**
   - **Locations**
   - **Jobs**
   - **Appointments**
   - **Technicians**
   - **Leads** (used only if you use TradeRated's referral feature)

   > **Important:** make sure **Customers** and **Locations** are included, not
   > just Jobs and Technicians. Your technicians' job list needs customer
   > names and addresses, which live on those two records.

5. ServiceTitan will show you four values once the app is created:
   - **Client ID**
   - **Client Secret**
   - **App Key**
   - **Tenant ID** (this is usually your account/company number — ServiceTitan
     shows it in the same screen or in your account settings)

   **Copy all four somewhere safe for a moment** — some of these are shown
   only once.

## Step 2 — Paste your four values into this repository

1. In this GitHub repository, click **Settings** (top of the repository page,
   not your personal account settings).
2. In the left sidebar, click **Secrets and variables → Actions**.
3. Click **New repository secret** four times, once for each value:
   - Name: `ST_CLIENT_ID` — value: your Client ID
   - Name: `ST_CLIENT_SECRET` — value: your Client Secret
   - Name: `ST_APP_KEY` — value: your App Key
   - Name: `ST_TENANT_ID` — value: your Tenant ID

That's it for your half. TradeRated pre-fills a Google Sheet and its access
key for you — you don't create or manage that part.

## What happens next

- Within about 5–15 minutes, your jobs start appearing in a private Google
  Sheet that only TradeRated can read.
- Technicians start seeing today's jobs on their phones shortly after that.
- **Newly booked jobs take a few minutes to show up** — this system checks for
  updates on a schedule rather than instantly. GitHub's schedule has a
  five-minute minimum and is best-effort, so sometimes it'll be closer to ten
  or twelve minutes. This is expected, not a malfunction.

## A note on your secrets, honestly

Once you paste a secret into GitHub's repository settings, **nobody — not
GitHub, not TradeRated, not even a repository administrator — can read that
value back out.** That's what makes it safe for TradeRated to share this
repository with you.

The one thing to know: someone with **write access** to this repository could
still write a workflow that *uses* one of these secrets (for example, to print
it somewhere, or send it elsewhere). GitHub's write-only protection stops
casual viewing, but it does not stop a person who already has write access to
this specific repository from misusing a secret they can't see directly. Only
grant write access on this repository to people you trust with your
ServiceTitan credentials as a matter of policy — the same trust you'd extend to
anyone with a key to your ServiceTitan account.

## Revoking access

To disconnect at any time:
1. Go to ServiceTitan's Developer Portal and delete or deactivate the app you
   created in Step 1. This immediately stops all data flow — nothing further
   needs to happen on GitHub's side.
2. Optionally, delete the four secrets from this repository's settings too.

## Questions?

If something doesn't look right — no jobs appearing, a technician missing from
the list, or anything else — contact your TradeRated onboarding contact rather
than trying to fix repository settings yourself.
```

- [ ] **Step 2: Proofread against the acceptance criteria**

Confirm the document satisfies `issues/06`'s acceptance line: "The owner
completes their part in under ten minutes with no dev in the ServiceTitan
portal." Check: does Step 1 assume any technical knowledge beyond clicking
through a form? Does Step 2 assume familiarity with GitHub beyond "click
Settings, click New secret"? If either step requires something not spelled out
here, add it before committing.

- [ ] **Step 3: Commit**

```bash
git add SETUP.md
git commit -m "docs: add owner-facing SETUP.md for the ServiceTitan connection"
```

---

### Task 17: Final report

**Files:** none

- [ ] **Step 1: Confirm both repos' state**

In `ST-exporter` (this repo/worktree): `git log --oneline feat/servicetitan-hosted..HEAD`
should show one commit per Task 1–12 (9 commits total: Tasks 1, 2, 3, 4, 5, 6,
7, 8, 9, 10, 11, 12 — 12 commits; adjust the expected count if any step above
was folded together during actual execution) plus the local tag from Task 12.

In `tr-doorservpro` (the scratch clone): `git log --oneline main..HEAD` should
show 2 commits (Tasks 15, 16).

- [ ] **Step 2: Report to the user**

Summarize, without pushing anything:
- What was built, file by file, in both repos.
- What's verified (full test suite + lint + mypy in `ST-exporter`, since
  `tr-doorservpro`'s workflow YAML has no test harness — YAML syntax
  validation is the only mechanical check available for it).
- What's explicitly unverified/open, restated plainly:
  1. `technician_rating` outbox items will always fail until the spec owner
     names a real ServiceTitan write for it.
  2. The outbox response envelope (`{"items": [...]}`) and claim limit (10)
     are assumptions, not confirmed against a real TradeRated endpoint.
  3. Nothing has been pushed — `feat/servicetitan-hosted` in `ST-exporter` has
     new local commits and an unpushed tag `exporter-v0.2.0`; `tr-doorservpro`
     has a new local branch `feat/servicetitan-hosted` with two commits and no
     remote counterpart yet. The caller workflow cannot actually resolve
     `@exporter-v0.2.0` until both are pushed.
  4. The real 24-hour-unattended-run and full outbox-round-trip acceptance
     criteria from `issues/06` cannot be proven until ticket 07's Sheet/
     machine-token/outbox-URL secrets exist and Paul's ServiceTitan
     environment is live — this plan builds and unit-tests everything up to
     that point, per the brief's explicit allowance to do so.
- Ask explicitly whether to push `feat/servicetitan-hosted` (and the tag) to
  `BBTT-01/ST-exporter`, and whether/how to get `tr-doorservpro`'s branch
  pushed (it has no existing remote branch to push alongside).

## Self-Review Notes

**Spec coverage check**, against `issues/06-exporter-workflow-and-setup.md`:
- "`on: workflow_call` reusable workflow ... inputs (feeds, window, target,
  pricebook) and secrets (four ServiceTitan values, Sheets service-account key,
  TradeRated machine token)" → Task 11.
- "Caller workflow in `tr-doorservpro` ... pinned to a version tag ... Two
  crons — jobs every 5 minutes, technicians every 30" → Tasks 12, 15 (requires
  Tasks 1–3's `--feeds` work to be meaningful).
- "Outbox drain in the same run ... `GET /crm-outbox` ... perform ... presenting
  the `idempotency_key` ... `POST /crm-outbox/:id/result`" → Tasks 5, 6, 7, 8, 9.
- "`SETUP.md`: plain English, screenshot-driven ... owner's part is exactly two
  things" → Task 16 (screenshots themselves are out of this plan's reach — no
  live ServiceTitan portal to screenshot against; flagged as a follow-up, not
  silently dropped).
- "Record honestly in SETUP.md that GitHub repo secrets are write-only ... and
  that a collaborator could add a workflow that prints one" → Task 16's "A note
  on your secrets, honestly" section.
- "GitHub Actions cron has a five-minute floor and is best-effort. Do not
  promise 5 minutes." → Task 15's workflow comment and Task 16's SETUP.md
  wording both say "5–15 minutes" / "sometimes closer to ten or twelve," never
  a flat promise.

**Not covered by this plan, deliberately** (per the ticket's own scope and this
session's earlier readiness check): pushing anything to either remote;
obtaining real screenshots for `SETUP.md` (no live portal access); the
Marketplace-grant licensing question and the cross-org-workflow-visibility
question from `spec.md`'s "Further Notes" (those are `spec.md`-level open
items for the ticket owner, not implementation tasks); any `traderatedapp`
code (issues 01–04, explicitly another engineer's scope).

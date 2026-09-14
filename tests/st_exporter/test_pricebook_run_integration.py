"""End-to-end: `--feeds pricebook` through run_export, respx + in-memory Sheets.

This is the closest thing to the ticket's "against a real tenant" done-when that
is reachable without credentials: the whole orchestration runs, only the HTTP
transport is faked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import respx

from st_exporter.meta import parse_meta_grid
from st_exporter.pricebook import CATEGORY_COLUMNS, ITEM_COLUMNS
from st_exporter.run import run_export
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token
from tests.st_exporter.fixtures import tenant_pricebook

FIXED_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
PRICEBOOK_TABS = (
    "pricebook.services",
    "pricebook.equipment",
    "pricebook.materials",
    "pricebook.categories",
)


def _frozen_now():
    return patch("st_exporter.run.datetime", **{"now.return_value": FIXED_NOW})


def _run(st_settings, exporter_settings, export_store, feeds=frozenset({"pricebook"})):
    with _frozen_now():
        return run_export(
            st_settings,
            exporter_settings,
            feeds=feeds,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )


def _rows(grid):
    return [dict(zip(grid[0], row)) for row in grid[1:]]


@respx.mock
def test_writes_all_four_tabs_with_contract_headers(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    assert set(export_store.tabs) >= set(PRICEBOOK_TABS)
    for tab in PRICEBOOK_TABS[:3]:
        assert export_store.tabs[tab][0] == list(ITEM_COLUMNS)
    assert export_store.tabs["pricebook.categories"][0] == list(CATEGORY_COLUMNS)
    assert summary.pricebook_row_counts == {
        "pricebook.services": 2,
        "pricebook.equipment": 1,
        "pricebook.materials": 1,
        "pricebook.categories": 2,
    }


@respx.mock
def test_each_tab_gets_its_own_meta_row_with_the_contract_version(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    export_store = InMemorySheetsStore()

    _run(st_settings, exporter_settings, export_store)

    meta = parse_meta_grid(export_store.tabs["_meta"])
    for tab in PRICEBOOK_TABS:
        assert meta[tab].contract_version == "pricebook.v1"
        # Catalogue: full replace, so no cursor is ever carried.
        assert meta[tab].last_cursor == ""
        assert meta[tab].last_run_at == FIXED_NOW.isoformat()
        assert meta[tab].exporter_version
    assert meta["pricebook.services"].row_count == 2


@respx.mock
def test_cells_follow_the_contract_end_to_end(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    export_store = InMemorySheetsStore()

    _run(st_settings, exporter_settings, export_store)

    services = {row["st_id"]: row for row in _rows(export_store.tabs["pricebook.services"])}
    assert services["2"]["price"] == ""  # null price is blank, never 0
    assert services["2"]["active"] == "false"
    assert services["2"]["name"] == "Quote On Site"  # displayName null -> name
    assert services["1"]["price"] == "129"

    equipment = _rows(export_store.tabs["pricebook.equipment"])[0]
    assert equipment["category_ids"] == "10,11"
    assert equipment["category_names"] == "Service,Doors"
    # Repeated asset deduped; the storage-path form survives as an identifier.
    assert equipment["image_refs"] == "a1,Images/Pricebook/9f2c-uuid.jpg"
    assert equipment["modified_on"] == "2026-09-03T00:00:00Z"

    material = _rows(export_store.tabs["pricebook.materials"])[0]
    assert material["price"] == "0"  # a real zero is NOT blank

    categories = {row["st_id"]: row for row in _rows(export_store.tabs["pricebook.categories"])}
    assert categories["10"]["parent_id"] == ""
    assert categories["11"]["parent_id"] == "10"


@respx.mock
def test_full_replace_drops_items_that_left_the_catalogue(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    export_store = InMemorySheetsStore()

    tenant_pricebook.register(st_settings.api_base)
    _run(st_settings, exporter_settings, export_store)
    assert {row["st_id"] for row in _rows(export_store.tabs["pricebook.services"])} == {"1", "2"}

    tenant_pricebook.register(st_settings.api_base, services=[tenant_pricebook.SERVICE_1])
    _run(st_settings, exporter_settings, export_store)
    assert {row["st_id"] for row in _rows(export_store.tabs["pricebook.services"])} == {"1"}


@respx.mock
def test_dry_run_writes_nothing_but_still_reports_counts(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    export_store = InMemorySheetsStore()

    with _frozen_now():
        summary = run_export(
            st_settings,
            exporter_settings,
            feeds=frozenset({"pricebook"}),
            dry_run=True,
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )

    assert summary.pricebook_row_counts["pricebook.services"] == 2
    assert export_store.tabs == {}


@respx.mock
def test_running_only_pricebook_leaves_other_feeds_meta_untouched(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    export_store = InMemorySheetsStore()
    export_store.replace_grid(
        "_meta",
        [
            ["feed", "last_run_at", "last_cursor", "row_count", "exporter_version"],
            ["jobs", "t-earlier", '{"jobs": "tok"}', "7", "0.2.7"],
        ],
    )

    _run(st_settings, exporter_settings, export_store)

    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert meta["jobs"].last_run_at == "t-earlier"
    assert meta["jobs"].last_cursor == '{"jobs": "tok"}'
    assert meta["jobs"].row_count == 7
    assert meta["jobs"].contract_version == ""
    assert "pricebook.services" in meta


@respx.mock
def test_not_selecting_pricebook_leaves_its_tabs_and_meta_untouched(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    export_store = InMemorySheetsStore()
    _run(st_settings, exporter_settings, export_store)
    before = [row[:] for row in export_store.tabs["pricebook.services"]]

    # A technicians-only run must not touch the pricebook tabs or drop their
    # _meta rows.
    respx.get(f"{st_settings.api_base}/settings/v2/tenant/12345/technicians").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": False})
    )
    summary = _run(st_settings, exporter_settings, export_store, feeds=frozenset({"technicians"}))

    assert summary.pricebook_row_counts is None
    assert export_store.tabs["pricebook.services"] == before
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert meta["pricebook.services"].contract_version == "pricebook.v1"

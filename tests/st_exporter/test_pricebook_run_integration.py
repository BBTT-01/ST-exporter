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
        assert meta[tab].contract_version == "pricebook.v2"
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
    respx.get(f"{st_settings.api_base}/settings/v2/tenant/12345/business-units").mock(
        return_value=httpx.Response(200, json={"data": [], "hasMore": False})
    )
    summary = _run(st_settings, exporter_settings, export_store, feeds=frozenset({"technicians"}))

    assert summary.pricebook_row_counts is None
    assert export_store.tabs["pricebook.services"] == before
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert meta["pricebook.services"].contract_version == "pricebook.v2"


# --- image upload lane -------------------------------------------------------
#
# The pricebook feed carries a second, optional job: POST the image bytes to
# TrueQuote. These run the whole of `run_export` with an image client attached,
# so what's exercised is the wiring (feed -> assets -> download -> POST ->
# ledger), not just the uploader in isolation.

TQ_BASE = "https://truequote.example.com/api/outbox"
TQ_UPLOAD = f"{TQ_BASE}/pricebook-image"
# tenant_pricebook's EQUIPMENT_1 carries this as its default asset.
EQUIPMENT_IMAGE_URL = "https://cdn.example.com/a1.jpg"
# Enough bytes that these fixtures clear `MIN_PLAUSIBLE_IMAGE_BYTES`. A real
# pricebook photograph is kilobytes; a byte-valid image under the floor is a
# blank placeholder and is refused on purpose (`assets.is_placeholder_image`),
# so a fixture standing in for a REAL image has to look like one.
_REAL_IMAGE_PADDING = b"\x00" * 2048

PNG = b"\x89PNG\r\n\x1a\n" + b"body" + _REAL_IMAGE_PADDING


def _image_client():
    from st_exporter.images.client import TrueQuoteImageClient

    return TrueQuoteImageClient(TQ_BASE, "tqm_image-token")


def _run_with_images(st_settings, exporter_settings, export_store, raw_cache_store, client):
    with _frozen_now():
        return run_export(
            st_settings,
            exporter_settings,
            feeds=frozenset({"pricebook"}),
            export_store=export_store,
            raw_cache_store=raw_cache_store,
            image_client=client,
        )


@respx.mock
def test_pricebook_run_uploads_image_bytes_and_records_them(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    respx.get(EQUIPMENT_IMAGE_URL).mock(return_value=httpx.Response(200, content=PNG))
    respx.get(f"{st_settings.api_base}/pricebook/v2/tenant/12345/images").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    upload = respx.post(TQ_UPLOAD).mock(
        return_value=httpx.Response(
            200, json={"asset_key": "100:a1", "storage_path": "co/st/x.png", "status": "stored"}
        )
    )
    export_store, raw_cache_store = InMemorySheetsStore(), InMemorySheetsStore()

    client = _image_client()
    try:
        summary = _run_with_images(
            st_settings, exporter_settings, export_store, raw_cache_store, client
        )
    finally:
        client.close()

    assert summary.images is not None
    assert summary.images.uploaded == 1
    assert upload.calls[0].request.content == PNG
    # The identifiers still went to the Sheet; the bytes never did.
    assert "image_refs" in export_store.tabs["pricebook.equipment"][0]
    assert b"PNG" not in repr(export_store.tabs["pricebook.equipment"]).encode()
    # And the run remembered the upload so the next one can skip it.
    assert raw_cache_store.tabs["_image_ledger"][1][0]


@respx.mock
def test_a_refused_upload_does_not_abort_the_run(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    respx.get(EQUIPMENT_IMAGE_URL).mock(return_value=httpx.Response(200, content=PNG))
    respx.post(TQ_UPLOAD).mock(
        return_value=httpx.Response(422, json={"error": "image_content_mismatch"})
    )
    export_store, raw_cache_store = InMemorySheetsStore(), InMemorySheetsStore()

    client = _image_client()
    try:
        summary = _run_with_images(
            st_settings, exporter_settings, export_store, raw_cache_store, client
        )
    finally:
        client.close()

    # Every tab was still written and every row still counted.
    assert summary.pricebook_row_counts == {
        "pricebook.services": 2,
        "pricebook.equipment": 1,
        "pricebook.materials": 1,
        "pricebook.categories": 2,
    }
    assert set(export_store.tabs) >= set(PRICEBOOK_TABS)
    assert summary.images is not None and summary.images.upload_rejected == 1


@respx.mock
def test_servicetitan_403_on_images_is_reported_and_the_tabs_still_land(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    # An item whose only image is an AUTHENTICATED storage path — the form that
    # needs the `Pricebook -> Images` permission the contractor may not have
    # granted.
    tenant_pricebook.register(
        st_settings.api_base,
        equipment=[
            {
                **tenant_pricebook.EQUIPMENT_1,
                "assets": [{"id": None, "url": "Images/Pricebook/9f2c-uuid.jpg"}],
            }
        ],
    )
    respx.get(f"{st_settings.api_base}/pricebook/v2/tenant/12345/images").mock(
        return_value=httpx.Response(403, text="Pricebook Images permission missing")
    )
    respx.post(TQ_UPLOAD).mock(
        return_value=httpx.Response(
            200, json={"asset_key": "k", "storage_path": "p", "status": "stored"}
        )
    )
    export_store, raw_cache_store = InMemorySheetsStore(), InMemorySheetsStore()

    client = _image_client()
    try:
        summary = _run_with_images(
            st_settings, exporter_settings, export_store, raw_cache_store, client
        )
    finally:
        client.close()

    assert summary.images is not None
    assert summary.images.permission_denied is True
    assert set(export_store.tabs) >= set(PRICEBOOK_TABS)


@respx.mock
def test_no_image_client_means_no_upload_pass_at_all(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    # None, not an empty summary: "didn't look" and "nothing to send" differ.
    assert summary.images is None


# --- per-tab isolation -------------------------------------------------------


@respx.mock
def test_one_failing_tab_does_not_cost_the_other_three(st_settings, exporter_settings) -> None:
    """The same bar the financial feed meets: a tab failing costs that tab.

    There is no atomicity to protect between the four — they are four separate
    full replaces a consumer joins by id — so losing services, materials AND
    categories because equipment 500'd is pure self-inflicted damage.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    respx.get(f"{st_settings.api_base}/pricebook/v2/tenant/12345/equipment").mock(
        return_value=httpx.Response(500, text="pricebook is unwell")
    )
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    assert set(summary.pricebook_row_counts) == {
        "pricebook.services",
        "pricebook.materials",
        "pricebook.categories",
    }
    assert "pricebook.equipment" in summary.pricebook_failures
    assert "pricebook.equipment" not in export_store.tabs
    assert export_store.tabs["pricebook.services"][0] == list(ITEM_COLUMNS)


@respx.mock
def test_a_failed_tab_keeps_its_previous_contents_and_meta_row(
    st_settings, exporter_settings
) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    export_store = InMemorySheetsStore()
    # Run once cleanly so there is a previous state to preserve.
    _run(st_settings, exporter_settings, export_store)
    before = [row[:] for row in export_store.tabs["pricebook.equipment"]]

    respx.get(f"{st_settings.api_base}/pricebook/v2/tenant/12345/equipment").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    with patch("st_cli.client.time.sleep"):
        summary = _run(st_settings, exporter_settings, export_store)

    # Untouched, not emptied — and its _meta row still says when it was last
    # genuinely refreshed rather than claiming a run that produced nothing.
    assert export_store.tabs["pricebook.equipment"] == before
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert meta["pricebook.equipment"].row_count == 1
    assert "pricebook.equipment" in summary.pricebook_failures
    # A transport failure, which is not an HTTP status, is caught like any other.
    assert "ConnectError" in summary.pricebook_failures["pricebook.equipment"]


@respx.mock
def test_a_clean_run_reports_no_failures(st_settings, exporter_settings) -> None:
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    summary = _run(st_settings, exporter_settings, InMemorySheetsStore())
    assert summary.pricebook_failures == {}


@respx.mock
def test_an_item_with_no_st_id_is_neither_written_nor_counted(
    st_settings, exporter_settings
) -> None:
    # `st_id` is non-empty by contract, and a blank key column in a written tab
    # is indistinguishable from real data at a glance while `row_count` counts
    # it anyway. The category-FILTERED fetch path already drops these.
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(
        st_settings.api_base,
        services=[tenant_pricebook.SERVICE_1, {**tenant_pricebook.SERVICE_2_NO_PRICE, "id": None}],
        categories=[tenant_pricebook.CATEGORY_10, {"name": "orphan", "active": True}],
    )
    export_store = InMemorySheetsStore()

    summary = _run(st_settings, exporter_settings, export_store)

    assert [row[0] for row in export_store.tabs["pricebook.services"][1:]] == ["1"]
    assert summary.pricebook_row_counts["pricebook.services"] == 1
    assert summary.pricebook_row_counts["pricebook.categories"] == 1
    # The count in `_meta` is what was actually written, not what was fetched.
    meta = parse_meta_grid(export_store.tabs["_meta"])
    assert meta["pricebook.services"].row_count == 1


# --- the image lane is a SIDE lane -------------------------------------------


class _RefusingLedgerStore(InMemorySheetsStore):
    """A raw-cache Sheet that fails exactly on the image ledger tab."""

    def read_grid(self, tab_name: str):
        if tab_name == "_image_ledger":
            raise RuntimeError("the raw cache sheet is unreachable")
        return super().read_grid(tab_name)


@respx.mock
def test_an_exploding_image_lane_never_costs_the_four_tabs_their_meta(
    st_settings, exporter_settings
) -> None:
    """Four fresh tabs described by a STALE `_meta` row is the worst outcome here.

    The tabs are written before the image pass and `_meta` after it, so anything
    escaping the image lane leaves wrong `row_count`/`last_run_at` for all four,
    forgets every upload the pass already made, and skips the outbox drain.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    respx.get(EQUIPMENT_IMAGE_URL).mock(return_value=httpx.Response(200, content=PNG))
    respx.post(TQ_UPLOAD).mock(
        return_value=httpx.Response(
            200, json={"asset_key": "k", "storage_path": "p", "status": "stored"}
        )
    )
    export_store, raw_cache_store = InMemorySheetsStore(), _RefusingLedgerStore()

    client = _image_client()
    try:
        summary = _run_with_images(
            st_settings, exporter_settings, export_store, raw_cache_store, client
        )
    finally:
        client.close()

    meta = parse_meta_grid(export_store.tabs["_meta"])
    for tab in PRICEBOOK_TABS:
        assert meta[tab].last_run_at == FIXED_NOW.isoformat()
    assert summary.pricebook_row_counts["pricebook.equipment"] == 1
    # Named, not swallowed: the failure is reported on the summary.
    assert summary.images is not None and summary.images.stopped is not None


@respx.mock
def test_uploads_already_made_are_flushed_even_when_the_pass_dies(
    st_settings, exporter_settings
) -> None:
    """The ledger is the ONLY record of what has already crossed the wire.

    Losing it re-sends bytes TrueQuote already has, every run, forever — so the
    flush belongs in a `finally`, not on the happy path.
    """
    mock_auth_token(st_settings.auth_url)
    service_image_url = "https://cdn.example.com/service-1.jpg"
    tenant_pricebook.register(
        st_settings.api_base,
        # Two uploadable items, so there IS a first upload to lose.
        services=[
            {
                **tenant_pricebook.SERVICE_1,
                "assets": [{"id": "s1", "url": service_image_url, "isDefault": True}],
            }
        ],
    )
    respx.get(service_image_url).mock(return_value=httpx.Response(200, content=PNG))
    respx.get(EQUIPMENT_IMAGE_URL).mock(return_value=httpx.Response(200, content=PNG))
    respx.get(f"{st_settings.api_base}/pricebook/v2/tenant/12345/images").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    respx.post(TQ_UPLOAD).mock(
        return_value=httpx.Response(
            200, json={"asset_key": "k", "storage_path": "p", "status": "stored"}
        )
    )
    export_store, raw_cache_store = InMemorySheetsStore(), InMemorySheetsStore()

    client = _image_client()
    original = client.upload
    calls = {"n": 0}

    def explode_after_the_first(**kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("something nobody predicted")
        return original(**kwargs)

    client.upload = explode_after_the_first  # type: ignore[method-assign]
    try:
        summary = _run_with_images(
            st_settings, exporter_settings, export_store, raw_cache_store, client
        )
    finally:
        client.close()

    # The first upload really happened, so it must be remembered.
    assert len(raw_cache_store.tabs["_image_ledger"]) == 2
    assert summary.images is not None and summary.images.stopped is not None


# --- the side lane can never precede `_meta` ---------------------------------


class _FlushRefusingStore(InMemorySheetsStore):
    """A raw-cache Sheet that fails on the ledger WRITE, not the read.

    A Sheets 429 on `replace_grid("_image_ledger")` is routine, and it happens
    inside `ImageLedger.flush()` — which runs in a `finally`, so it replaces
    whatever the body was doing and walks straight past the lane's `except`.
    """

    def replace_grid(self, tab_name: str, grid) -> None:
        if tab_name == "_image_ledger":
            raise RuntimeError("Sheets 429: quota exceeded on the raw-cache sheet")
        return super().replace_grid(tab_name, grid)


class _OrderRecordingStore(InMemorySheetsStore):
    """Records whether `_meta` was already written when the image lane started."""

    def __init__(self, export_store: InMemorySheetsStore) -> None:
        super().__init__()
        self._export_store = export_store
        self.meta_written_when_the_image_lane_started: bool | None = None

    def read_grid(self, tab_name: str):
        if tab_name == "_image_ledger" and self.meta_written_when_the_image_lane_started is None:
            self.meta_written_when_the_image_lane_started = "_meta" in self._export_store.tabs
        return super().read_grid(tab_name)


def _register_images(st_settings) -> None:
    respx.get(EQUIPMENT_IMAGE_URL).mock(return_value=httpx.Response(200, content=PNG))
    respx.get(f"{st_settings.api_base}/pricebook/v2/tenant/12345/images").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    respx.post(TQ_UPLOAD).mock(
        return_value=httpx.Response(
            200, json={"asset_key": "k", "storage_path": "p", "status": "stored"}
        )
    )


@respx.mock
def test_a_failing_ledger_flush_still_leaves_every_tab_described_by_meta(
    st_settings, exporter_settings
) -> None:
    """The flush is in a `finally`; an exception there must not end the run.

    Guarding the lane's body alone was not enough: a `finally` that raises
    replaces the in-flight state and escapes the `except` above it, which left
    four fresh pricebook tabs with an absent or stale `_meta` and skipped the
    outbox drain.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    _register_images(st_settings)
    export_store, raw_cache_store = InMemorySheetsStore(), _FlushRefusingStore()

    client = _image_client()
    try:
        summary = _run_with_images(
            st_settings, exporter_settings, export_store, raw_cache_store, client
        )
    finally:
        client.close()

    meta = parse_meta_grid(export_store.tabs["_meta"])
    for tab in PRICEBOOK_TABS:
        assert meta[tab].last_run_at == FIXED_NOW.isoformat()
    # Named, never silent: losing the ledger costs re-uploaded bytes next run.
    assert summary.images is not None
    assert summary.images.stopped is not None
    assert "flush" in summary.images.stopped


@respx.mock
def test_the_image_lane_runs_after_meta_is_written(st_settings, exporter_settings) -> None:
    """Ordering, not handling, is what makes "fresh tabs, stale `_meta`" impossible.

    Any handling is a promise about the failures we thought of; running the side
    lane after `_meta` means a hang, a timeout-minutes kill or an unimagined
    exception cannot get between a written tab and the row describing it.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    _register_images(st_settings)
    export_store = InMemorySheetsStore()
    raw_cache_store = _OrderRecordingStore(export_store)

    client = _image_client()
    try:
        summary = _run_with_images(
            st_settings, exporter_settings, export_store, raw_cache_store, client
        )
    finally:
        client.close()

    assert summary.images is not None and summary.images.uploaded == 1
    assert raw_cache_store.meta_written_when_the_image_lane_started is True


@respx.mock
def test_a_failed_item_tab_stops_the_image_pass_pruning_the_ledger(
    st_settings, exporter_settings
) -> None:
    """A failed tab means its items never reached `item_records`.

    `ImageUploadSummary.complete` cannot see that — it only knows about the
    records it was handed — so `ledger.keep(seen_keys)` would drop every
    equipment image key as "gone" and re-send identical bytes next run,
    violating `ImageLedger.keep`'s own precondition.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    _register_images(st_settings)
    export_store, raw_cache_store = InMemorySheetsStore(), InMemorySheetsStore()

    client = _image_client()
    try:
        first = _run_with_images(
            st_settings, exporter_settings, export_store, raw_cache_store, client
        )
        assert first.images is not None and first.images.uploaded == 1
        ledger_after_first = list(raw_cache_store.tabs["_image_ledger"])

        respx.get(f"{st_settings.api_base}/pricebook/v2/tenant/12345/equipment").mock(
            return_value=httpx.Response(500, text="unwell")
        )
        second = _run_with_images(
            st_settings, exporter_settings, export_store, raw_cache_store, client
        )
        assert "pricebook.equipment" in second.pricebook_failures
        # The ledger is untouched: "not looked at" is not "gone".
        assert raw_cache_store.tabs["_image_ledger"] == ledger_after_first

        tenant_pricebook.register(st_settings.api_base)
        third = _run_with_images(
            st_settings, exporter_settings, export_store, raw_cache_store, client
        )
        assert third.images is not None
        assert third.images.uploaded == 0
        assert third.images.already_uploaded == 1
    finally:
        client.close()

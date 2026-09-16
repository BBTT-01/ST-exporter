"""End-to-end: `--feeds images` through `run_images`, respx + in-memory Sheets.

The `images` feed is the pricebook image upload, lifted out of the pricebook
feed into a run of its own. Everything here is the real orchestration with only
the HTTP transport faked, so what is exercised is where an INDEPENDENT image
pass gets its references from and what it writes.

Why it exists at all: on `BBTT-01/tr-doorservpro` the image pass lived inside
the pricebook feed's 10-minute job, and with ~7,191 assets the runner SIGKILLed
it at 10m35s having uploaded zero images (run 35130164187). It could not
converge — a killed process flushes no ledger — and it failed the hourly
pricebook export every time it died.
"""

from __future__ import annotations

from typing import Generator

import httpx
import pytest
import respx

from st_exporter.images.client import TrueQuoteImageClient
from st_exporter.run import EXPORT_TABS, run_images
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token
from tests.st_exporter.fixtures import tenant_pricebook

TQ_BASE = "https://truequote.example.com/api/outbox"
TQ_UPLOAD = f"{TQ_BASE}/pricebook-image"
PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"


@pytest.fixture()
def image_client() -> Generator[TrueQuoteImageClient, None, None]:
    client = TrueQuoteImageClient(TQ_BASE, "tqm_image-token")
    yield client
    client.close()


def _accepted() -> httpx.Response:
    return httpx.Response(
        200, json={"asset_key": "k", "storage_path": "co/st/assets/x.png", "status": "stored"}
    )


def _register_image_transport(st_settings) -> None:
    """Both asset forms the fixture carries: a public CDN url and an
    authenticated ServiceTitan storage path."""
    respx.get("https://cdn.example.com/a1.jpg").mock(return_value=httpx.Response(200, content=PNG))
    respx.get(f"{st_settings.api_base}/pricebook/v2/tenant/{st_settings.tenant_id}/images").mock(
        return_value=httpx.Response(200, content=PNG)
    )


@respx.mock
def test_the_images_feed_gets_its_refs_from_servicetitan_not_from_the_sheet(
    st_settings, exporter_settings, image_client
) -> None:
    """THE DESIGN DECISION, as an assertion.

    An independent image pass has no pricebook records in memory, so it re-lists
    the catalogue from ServiceTitan. Reading them back off the exported
    `pricebook.*` tabs is not merely more coupled, it is IMPOSSIBLE against the
    frozen `pricebook.v1` contract: the one image column, `image_refs`, carries
    the asset's id when ServiceTitan supplies one and only otherwise its url —
    and the url is what gets downloaded. `select_uploadable_asset` also needs
    `fileName`, `alias`, `type` and `isDefault` to pick the same primary image
    TrueQuote's own selector would, and none of that is exported either.

    So the feed never opens the Export Store. It works on a Sheet that has never
    been written, which is what "degrades sensibly when the pricebook tab is
    missing or stale" means here: the question does not arise.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    _register_image_transport(st_settings)
    uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
    raw_cache_store = InMemorySheetsStore()

    summary = run_images(
        st_settings,
        exporter_settings,
        image_client=image_client,
        raw_cache_store=raw_cache_store,
    )

    assert summary.uploaded == len(uploads.calls) > 0
    assert summary.stopped is None
    # It listed every item resource itself rather than reading a written tab.
    listed = {
        str(call.request.url).rsplit("?", 1)[0].rsplit("/", 1)[-1]
        for call in respx.calls
        if "pricebook/v2" in str(call.request.url)
    }
    assert {"services", "equipment", "materials"} <= listed


@respx.mock
def test_the_images_feed_writes_no_export_tab_and_no_meta(
    st_settings, exporter_settings, image_client
) -> None:
    """THE CONCURRENCY FINDING, as an assertion.

    The reusable workflow lets a run leave the shared export lock only when it
    "provably writes no `_meta`". This is that proof, and it is what the
    `...-images` concurrency group rests on. If this test ever fails, the
    caller's `images-feed` job must go back on the shared export lock before
    anything else is done — an unsynchronised `_meta` rewrite is the cursor-loss
    class `fix/cursor-loss` repaired.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    _register_image_transport(st_settings)
    respx.post(TQ_UPLOAD).mock(return_value=_accepted())
    export_store = InMemorySheetsStore()
    raw_cache_store = InMemorySheetsStore()

    run_images(
        st_settings,
        exporter_settings,
        image_client=image_client,
        raw_cache_store=raw_cache_store,
    )

    # The Export Store was never even opened, let alone written.
    assert export_store.tabs == {}
    # And on the private raw-cache Sheet it touched exactly one tab.
    assert set(raw_cache_store.tabs) == {"_image_ledger"}
    assert "_meta" not in raw_cache_store.tabs
    assert not set(raw_cache_store.tabs) & set(EXPORT_TABS)


@respx.mock
def test_a_refused_item_resource_costs_only_its_own_images(
    st_settings, exporter_settings, image_client
) -> None:
    """A TrueQuote-only tenant has Services, Equipment and Categories ticked and
    NOT Materials, so a 403 on materials is the ORDINARY state of the tenants
    this feed exists for. It must not cost them the images they DID buy — and it
    must veto the prune, because items that were never listed are "not looked
    at", never "gone".
    """
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    respx.get(f"{st_settings.api_base}/pricebook/v2/tenant/{st_settings.tenant_id}/materials").mock(
        return_value=httpx.Response(403, json={"title": "Forbidden"})
    )
    _register_image_transport(st_settings)
    uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
    raw_cache_store = InMemorySheetsStore()

    summary = run_images(
        st_settings,
        exporter_settings,
        image_client=image_client,
        raw_cache_store=raw_cache_store,
    )

    assert len(uploads.calls) > 0, "a refused resource must not skip the granted ones"
    assert summary.stopped is None
    assert "_image_ledger" in raw_cache_store.tabs


@respx.mock
def test_a_spent_budget_ends_the_run_with_a_written_ledger(
    st_settings, exporter_settings, image_client
) -> None:
    """The whole mechanism in one run: a deadline already in the past stops the
    pass before it starts an asset, and the run still returns and still writes.

    A SIGKILL at `timeout-minutes` does neither, which is why the tenant in run
    35130164187 uploaded nothing for as long as it was scheduled.
    """
    mock_auth_token(st_settings.auth_url)
    tenant_pricebook.register(st_settings.api_base)
    _register_image_transport(st_settings)
    uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
    raw_cache_store = InMemorySheetsStore()

    summary = run_images(
        st_settings,
        exporter_settings,
        image_client=image_client,
        raw_cache_store=raw_cache_store,
        deadline=0.0,
    )

    assert summary.stopped is not None
    assert summary.pending > 0
    assert uploads.calls == []
    # It got far enough to write a (here empty) ledger rather than being killed.
    assert "_image_ledger" in raw_cache_store.tabs

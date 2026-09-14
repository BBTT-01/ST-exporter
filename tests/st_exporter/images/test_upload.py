"""Tests for the image upload pass: both identifier forms, replay, 403, failures.

Real ``ServiceTitanClient`` and real ``TrueQuoteImageClient`` throughout — only
the transport is faked (respx) and the Sheet is in memory, so what is exercised
is the actual download-and-POST path, not a mock of it.
"""

from __future__ import annotations

from typing import Generator

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_exporter.images.client import TrueQuoteImageClient
from st_exporter.images.ledger import ImageLedger
from st_exporter.images.upload import upload_pricebook_images
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token

TQ_BASE = "https://truequote.example.com/api/outbox"
TQ_UPLOAD = f"{TQ_BASE}/pricebook-image"
PUBLIC_URL = "https://cdn.example.com/a1.jpg"
STORAGE_PATH = "Images/Pricebook/9f2c-uuid.jpg"
NOW = "2026-09-14T12:00:00+00:00"

PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"
JPEG = b"\xff\xd8\xff" + b"jpeg-body"

PUBLIC_ITEM = {"id": 100, "assets": [{"id": "a1", "url": PUBLIC_URL, "isDefault": True}]}
STORAGE_ITEM = {"id": 200, "assets": [{"id": None, "url": STORAGE_PATH}]}
NO_IMAGE_ITEM = {"id": 300, "assets": []}


@pytest.fixture()
def image_client() -> Generator[TrueQuoteImageClient, None, None]:
    client = TrueQuoteImageClient(TQ_BASE, "tqm_image-token")
    yield client
    client.close()


@pytest.fixture()
def st_client(st_settings) -> Generator[ServiceTitanClient, None, None]:
    client = ServiceTitanClient(st_settings)
    yield client
    client.close()


def _images_url(st_settings) -> str:
    return f"{st_settings.api_base}/pricebook/v2/tenant/{st_settings.tenant_id}/images"


def _accepted(status: str = "stored") -> httpx.Response:
    return httpx.Response(
        200, json={"asset_key": "k", "storage_path": "co/st/assets/x.png", "status": status}
    )


def _run(st_client, image_client, records, ledger=None):
    return upload_pricebook_images(
        st_client, image_client, ledger or ImageLedger(InMemorySheetsStore()), records, now=NOW
    )


class TestBothIdentifierForms:
    @respx.mock
    def test_public_https_asset_is_fetched_directly_and_uploaded(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [PUBLIC_ITEM])

        assert (summary.uploaded, summary.considered) == (1, 1)
        request = upload.calls[0].request
        assert request.content == PNG
        assert request.headers["content-type"] == "image/png"
        assert request.url.params["source_url"] == PUBLIC_URL
        assert request.url.params["external_item_id"] == "100"

    @respx.mock
    def test_storage_path_goes_through_servicetitans_authenticated_images_endpoint(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        images = respx.get(_images_url(st_settings)).mock(
            return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        )
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [STORAGE_ITEM])

        assert summary.uploaded == 1
        # The `path=` query parameter is the one TrueQuote's own client uses.
        assert images.calls[0].request.url.params["path"] == STORAGE_PATH
        assert images.calls[0].request.headers["authorization"] == "Bearer test-token"
        assert upload.calls[0].request.content == JPEG
        assert upload.calls[0].request.headers["content-type"] == "image/jpeg"

    @respx.mock
    def test_item_with_no_usable_image_is_counted_and_skipped(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [NO_IMAGE_ITEM])

        assert (summary.no_image, summary.considered, summary.uploaded) == (1, 0, 0)
        assert not upload.called


class TestIdempotency:
    @respx.mock
    def test_a_second_run_over_unchanged_bytes_uploads_nothing(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()

        first = ImageLedger(store)
        _run(st_client, image_client, [PUBLIC_ITEM], first)
        first.flush()

        second_summary = _run(st_client, image_client, [PUBLIC_ITEM], ImageLedger(store))

        assert upload.call_count == 1
        assert second_summary.already_uploaded == 1
        assert second_summary.uploaded == 0

    @respx.mock
    def test_changed_bytes_are_re_uploaded(self, st_settings, st_client, image_client) -> None:
        mock_auth_token(st_settings.auth_url)
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted("replaced"))
        store = InMemorySheetsStore()

        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        ledger = ImageLedger(store)
        _run(st_client, image_client, [PUBLIC_ITEM], ledger)
        ledger.flush()

        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG + b"edited"))
        summary = _run(st_client, image_client, [PUBLIC_ITEM], ImageLedger(store))

        assert upload.call_count == 2
        assert summary.uploaded == 1

    @respx.mock
    def test_the_key_on_the_wire_is_stable_across_runs(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        _run(st_client, image_client, [PUBLIC_ITEM])
        _run(st_client, image_client, [PUBLIC_ITEM])

        keys = {call.request.headers["idempotency-key"] for call in upload.calls}
        assert len(keys) == 1


class TestServiceTitanImagePermission:
    @respx.mock
    def test_403_is_a_named_non_fatal_outcome(self, st_settings, st_client, image_client) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(_images_url(st_settings)).mock(return_value=httpx.Response(403, text="forbidden"))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [STORAGE_ITEM])

        assert summary.permission_denied is True
        assert summary.complete is False
        assert summary.uploaded == 0
        assert summary.download_failed == 0  # named separately, not lumped in with errors
        assert not upload.called

    @respx.mock
    def test_403_does_not_stop_public_images_or_the_run(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        images = respx.get(_images_url(st_settings)).mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        second_storage_item = {"id": 201, "assets": [{"id": None, "url": STORAGE_PATH}]}
        summary = _run(st_client, image_client, [STORAGE_ITEM, PUBLIC_ITEM, second_storage_item])

        assert summary.permission_denied is True
        assert summary.uploaded == 1  # the public one still went
        # The permission is tenant-wide: asked once, not once per item.
        assert images.call_count == 1
        assert upload.call_count == 1


class TestFailuresDoNotAbortTheRun:
    @respx.mock
    def test_a_422_refusal_is_counted_and_the_next_item_still_uploads(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.get(_images_url(st_settings)).mock(return_value=httpx.Response(200, content=JPEG))
        respx.post(TQ_UPLOAD).mock(
            side_effect=[
                httpx.Response(422, json={"error": "image_content_mismatch"}),
                _accepted(),
            ]
        )

        summary = _run(st_client, image_client, [PUBLIC_ITEM, STORAGE_ITEM])

        assert summary.upload_rejected == 1
        assert summary.uploaded == 1

    @respx.mock
    def test_a_broken_image_link_is_counted_and_stepped_over(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(404, text="gone"))
        respx.get(_images_url(st_settings)).mock(return_value=httpx.Response(200, content=JPEG))
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [PUBLIC_ITEM, STORAGE_ITEM])

        assert summary.download_failed == 1
        assert summary.uploaded == 1

    @respx.mock
    def test_a_503_stops_the_pass_and_leaves_the_rest_for_next_run(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        images = respx.get(_images_url(st_settings)).mock(
            return_value=httpx.Response(200, content=JPEG)
        )
        upload = respx.post(TQ_UPLOAD).mock(
            return_value=httpx.Response(503, json={"error": "outbox_unavailable"})
        )

        summary = _run(st_client, image_client, [PUBLIC_ITEM, STORAGE_ITEM])

        assert summary.stopped == "outbox_unavailable"
        assert summary.complete is False
        assert upload.call_count == 1
        assert not images.called  # the second item was never even downloaded

    @respx.mock
    def test_bytes_that_are_not_an_image_are_never_sent(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=b"GIF89a-not-allowed"))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [PUBLIC_ITEM])

        assert summary.unsupported == 1
        assert not upload.called

    @respx.mock
    def test_an_oversized_image_is_never_sent(self, st_settings, st_client, image_client) -> None:
        mock_auth_token(st_settings.auth_url)
        oversized = b"\x89PNG\r\n\x1a\n" + b"x" * (8 * 1024 * 1024)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=oversized))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [PUBLIC_ITEM])

        assert summary.too_large == 1
        assert not upload.called

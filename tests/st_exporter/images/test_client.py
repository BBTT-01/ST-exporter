"""Tests for TrueQuoteImageClient — the exact wire format of the POST.

These tests are the written-down version of
``apps/admin/app/api/outbox/pricebook-image/route.ts``. If TrueQuote changes that
route, these fail, which is the intent: the format is theirs, not ours.
"""

from __future__ import annotations

from typing import Generator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from st_exporter.images.client import (
    ImageUploadAccepted,
    ImageUploadRejected,
    TrueQuoteImageClient,
)

BASE_URL = "https://truequote.example.com/api/outbox"
UPLOAD_URL = f"{BASE_URL}/pricebook-image"
PNG = b"\x89PNG\r\n\x1a\n" + b"body"


@pytest.fixture()
def client() -> Generator[TrueQuoteImageClient, None, None]:
    c = TrueQuoteImageClient(BASE_URL, "tqm_test-image-token")
    yield c
    c.close()


def _ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "asset_key": "100:a1",
            "storage_path": "co/servicetitan/assets/deadbeef.png",
            "status": "stored",
        },
    )


def _upload(client: TrueQuoteImageClient, **overrides):
    kwargs = {
        "external_item_id": "100",
        "source_url": "Images/Pricebook/9f2c.jpg",
        "content_type": "image/png",
        "payload": PNG,
        "idempotency_key": "key-1",
        "asset_id": "a1",
        "filename": "door.jpg",
        "alias": "Front",
        "asset_type": "Image",
        "is_default": True,
    }
    kwargs.update(overrides)
    return client.upload(**kwargs)  # type: ignore[arg-type]


class TestWireFormat:
    @respx.mock
    def test_posts_raw_bytes_with_metadata_in_the_query_string(
        self, client: TrueQuoteImageClient
    ) -> None:
        route = respx.post(UPLOAD_URL).mock(return_value=_ok())

        _upload(client)

        request = route.calls[0].request
        assert request.method == "POST"
        assert urlparse(str(request.url)).path == "/api/outbox/pricebook-image"
        query = parse_qs(urlparse(str(request.url)).query)
        assert query == {
            "external_item_id": ["100"],
            "source_url": ["Images/Pricebook/9f2c.jpg"],
            "asset_id": ["a1"],
            "filename": ["door.jpg"],
            "alias": ["Front"],
            "asset_type": ["Image"],
            "is_default": ["true"],
        }
        # The body is the image and nothing else — no multipart, no base64.
        assert request.content == PNG
        assert request.headers["content-type"] == "image/png"
        assert request.headers["authorization"] == "Bearer tqm_test-image-token"

    @respx.mock
    def test_is_default_false_is_omitted_not_sent_as_false(
        self, client: TrueQuoteImageClient
    ) -> None:
        # route.ts reads `params.get('is_default') === 'true'`, so the only
        # meaningful values are "present and true" or absent.
        route = respx.post(UPLOAD_URL).mock(return_value=_ok())
        _upload(client, is_default=False)
        assert "is_default" not in str(route.calls[0].request.url)

    @respx.mock
    def test_absent_optional_metadata_is_omitted(self, client: TrueQuoteImageClient) -> None:
        route = respx.post(UPLOAD_URL).mock(return_value=_ok())
        _upload(client, asset_id=None, filename=None, alias=None, asset_type=None)
        query = parse_qs(urlparse(str(route.calls[0].request.url)).query)
        assert set(query) == {"external_item_id", "source_url", "is_default"}

    @respx.mock
    def test_carries_the_idempotency_key_header(self, client: TrueQuoteImageClient) -> None:
        # TrueQuote does not read this today — its dedupe is derived from
        # source_url server-side. Sent so the stable key is on the wire.
        route = respx.post(UPLOAD_URL).mock(return_value=_ok())
        _upload(client, idempotency_key="stable-key")
        assert route.calls[0].request.headers["idempotency-key"] == "stable-key"


class TestResponses:
    @respx.mock
    def test_200_is_parsed_into_the_accepted_result(self, client: TrueQuoteImageClient) -> None:
        respx.post(UPLOAD_URL).mock(return_value=_ok())
        result = _upload(client)
        assert result == ImageUploadAccepted(
            asset_key="100:a1",
            storage_path="co/servicetitan/assets/deadbeef.png",
            status="stored",
        )

    @respx.mock
    def test_replaced_status_is_carried_through(self, client: TrueQuoteImageClient) -> None:
        respx.post(UPLOAD_URL).mock(
            return_value=httpx.Response(
                200, json={"asset_key": "k", "storage_path": "p", "status": "replaced"}
            )
        )
        result = _upload(client)
        assert isinstance(result, ImageUploadAccepted) and result.status == "replaced"

    @respx.mock
    @pytest.mark.parametrize(
        "error",
        [
            "invalid_image_metadata",
            "image_too_large",
            "empty_image",
            "image_content_mismatch",
            "image_upload_not_hosted",
        ],
    )
    def test_422_is_permanent(self, client: TrueQuoteImageClient, error: str) -> None:
        respx.post(UPLOAD_URL).mock(return_value=httpx.Response(422, json={"error": error}))
        result = _upload(client)
        assert result == ImageUploadRejected(status_code=422, error=error, retryable=False)
        assert result.kind == "permanent"

    @respx.mock
    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (401, "unauthorized"),
            (503, "machine_token_unavailable"),
            (503, "outbox_unavailable"),
            (503, "crm_image_store_unavailable"),
            (429, "rate_limited"),
        ],
    )
    def test_connection_level_failures_are_retryable(
        self, client: TrueQuoteImageClient, status: int, error: str
    ) -> None:
        respx.post(UPLOAD_URL).mock(return_value=httpx.Response(status, json={"error": error}))
        result = _upload(client)
        assert result == ImageUploadRejected(status_code=status, error=error, retryable=True)

    @respx.mock
    def test_a_non_json_error_body_does_not_crash(self, client: TrueQuoteImageClient) -> None:
        # A proxy's HTML 502 never reaches route.ts and is not JSON.
        respx.post(UPLOAD_URL).mock(return_value=httpx.Response(502, text="<html>bad gateway"))
        result = _upload(client)
        assert result == ImageUploadRejected(status_code=502, error="http_502", retryable=True)

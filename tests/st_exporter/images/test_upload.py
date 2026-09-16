"""Tests for the image upload pass: both identifier forms, replay, 403, failures.

Real ``ServiceTitanClient`` and real ``TrueQuoteImageClient`` throughout — only
the transport is faked (respx) and the Sheet is in memory, so what is exercised
is the actual download-and-POST path, not a mock of it.
"""

from __future__ import annotations

from time import monotonic
from typing import Generator
from unittest.mock import patch

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_exporter.images.client import TrueQuoteImageClient
from st_exporter.images.ledger import ImageLedger, ImageLedgerEntry
from st_exporter.images.upload import BUDGET_SPENT, upload_pricebook_images
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


def _run(st_client, image_client, records, ledger=None, deadline=None):
    return upload_pricebook_images(
        st_client,
        image_client,
        ledger or ImageLedger(InMemorySheetsStore()),
        records,
        now=NOW,
        deadline=deadline,
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


class TestTrueQuoteUnreachable:
    """A TrueQuote request that never reaches a status must not raise.

    ``upload.py``'s failure policy promises "401/429/5xx ends the pass, not the
    run". A DNS failure or a read timeout is an exception, not a status, so it
    used to sail past that promise entirely — out of the image pass, out of
    ``run_export``, and past the `_meta` write for four tabs that had already
    been written.
    """

    @respx.mock
    def test_a_timeout_posting_to_truequote_ends_the_pass_not_the_run(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.post(TQ_UPLOAD).mock(side_effect=httpx.ReadTimeout("truequote is slow"))

        summary = _run(st_client, image_client, [PUBLIC_ITEM, STORAGE_ITEM])

        assert summary.stopped is not None
        assert "ReadTimeout" in summary.stopped
        assert summary.complete is False
        assert summary.uploaded == 0

    @respx.mock
    def test_a_dns_failure_is_retryable_never_a_verdict_on_the_asset(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.post(TQ_UPLOAD).mock(side_effect=httpx.ConnectError("name resolution failed"))

        summary = _run(st_client, image_client, [PUBLIC_ITEM])

        # Counted as "stopped", never as `upload_rejected` — nothing about the
        # ASSET was judged, so it must be retried next run.
        assert summary.upload_rejected == 0
        assert summary.stopped is not None


class TestLedgerPruning:
    """`complete` decides whether the ledger is pruned, so it must mean "saw it all"."""

    @respx.mock
    def test_a_failed_download_makes_the_pass_incomplete(
        self, st_settings, st_client, image_client
    ) -> None:
        # A CDN 500 means this asset's key never reached `seen_keys`. Pruning
        # against `seen_keys` anyway would forget an upload that genuinely
        # happened and re-send identical bytes next run — breaking the "a retry
        # of an unchanged image sends zero bytes" invariant.
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(500, text="cdn is unwell"))
        respx.get(_images_url(st_settings)).mock(return_value=httpx.Response(200, content=JPEG))
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [PUBLIC_ITEM, STORAGE_ITEM])

        assert summary.download_failed == 1
        assert summary.uploaded == 1
        assert summary.complete is False

    @respx.mock
    def test_a_clean_pass_is_complete_and_may_prune(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [PUBLIC_ITEM, NO_IMAGE_ITEM])

        assert summary.complete is True


class TestServiceTitanUnreachable:
    """A transport failure on the AUTHENTICATED endpoint is about the connection.

    `_download` used to swallow it per asset and carry on. Each such asset now
    costs a full retry budget of timeouts, so a handful of unreachable images
    burn more than the workflow's `timeout-minutes` and the run is killed —
    which is how a stale `_meta` row happens without anything raising. The 403
    already stops the pass for the same reason; so does this.
    """

    @respx.mock
    def test_a_timeout_on_the_images_endpoint_stops_the_pass(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        images = respx.get(_images_url(st_settings)).mock(
            side_effect=httpx.ReadTimeout("servicetitan never answered")
        )
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        import st_cli.client as client_module

        with patch.object(client_module.time, "sleep"):
            summary = _run(st_client, image_client, [STORAGE_ITEM, STORAGE_ITEM, PUBLIC_ITEM])

        assert summary.stopped is not None
        assert summary.complete is False
        # The second storage asset was never even attempted: one retry budget,
        # not one per asset.
        assert images.call_count == 4
        assert summary.uploaded == 0

    @respx.mock
    def test_a_broken_public_link_still_only_costs_that_one_asset(
        self, st_settings, st_client, image_client
    ) -> None:
        # Widen-only in the other direction: a dead CDN host is about that URL,
        # not about ServiceTitan, and must not end the pass.
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(side_effect=httpx.ConnectError("cdn host is gone"))
        respx.get(_images_url(st_settings)).mock(return_value=httpx.Response(200, content=JPEG))
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [PUBLIC_ITEM, STORAGE_ITEM])

        assert summary.stopped is None
        assert summary.download_failed == 1
        assert summary.uploaded == 1


class TestTheTimeBudget:
    """The runner does not stop a pass, it SIGKILLs the process.

    A killed process flushes no ledger, so every upload that run made is
    forgotten and re-sent by the next one — which is how a tenant with more
    images than one job can download uploaded nothing at all, on every run, for
    ever. The budget is what turns that into a clean partial pass.
    """

    @respx.mock
    def test_a_spent_budget_stops_before_a_single_download(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        public = respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [PUBLIC_ITEM], deadline=monotonic() - 1)

        assert summary.stopped == BUDGET_SPENT
        assert (summary.uploaded, summary.considered, summary.pending) == (0, 0, 1)
        assert not public.called
        assert not upload.called

    @respx.mock
    def test_a_budget_that_runs_out_mid_pass_keeps_what_it_did(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.get(_images_url(st_settings)).mock(return_value=httpx.Response(200, content=JPEG))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        ledger = ImageLedger(InMemorySheetsStore())

        # First reading is inside the budget, second is past it.
        with patch("st_exporter.images.upload.monotonic", side_effect=[0.0, 100.0]):
            summary = _run(
                st_client,
                image_client,
                [PUBLIC_ITEM, STORAGE_ITEM],
                ledger=ledger,
                deadline=50.0,
            )

        assert (summary.uploaded, summary.pending) == (1, 1)
        assert summary.stopped == BUDGET_SPENT
        assert upload.call_count == 1
        # What it did upload is in the ledger, so the next run does not re-send it.
        assert ledger.last_verified("100:a1") == NOW

    @respx.mock
    def test_a_budget_that_is_never_reached_changes_nothing(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [PUBLIC_ITEM], deadline=monotonic() + 3600)

        assert summary.stopped is None
        assert (summary.uploaded, summary.pending) == (1, 0)
        assert summary.complete is True

    @respx.mock
    def test_a_budget_stop_never_prunes_the_ledger(
        self, st_settings, st_client, image_client
    ) -> None:
        """`complete` is what vetoes the prune, and a budget stop is not complete.

        Pruning against a partial pass's `seen_keys` would forget every upload
        the run never got round to re-verifying and send all of it again.
        """
        mock_auth_token(st_settings.auth_url)
        summary = _run(st_client, image_client, [PUBLIC_ITEM], deadline=monotonic() - 1)

        assert summary.complete is False


class TestResumeOrder:
    """Oldest verification first, so a bounded pass resumes instead of restarting.

    Without this every run works the catalogue in the same order and a tenant
    whose images do not fit in one job re-treads the same prefix for ever: the
    images past it are never reached, however many runs go by.
    """

    @respx.mock
    def test_an_asset_the_ledger_has_never_seen_goes_first(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.get(_images_url(st_settings)).mock(return_value=httpx.Response(200, content=JPEG))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        ledger = ImageLedger(InMemorySheetsStore())
        ledger.record(
            ImageLedgerEntry(
                idempotency_key="whatever",
                asset_ref="100:a1",
                storage_path="p",
                verified_at="2026-09-16T08:00:00+00:00",
            )
        )

        # PUBLIC_ITEM is first in the catalogue but already verified, so the
        # single asset this budget affords must be the one never seen.
        with patch("st_exporter.images.upload.monotonic", side_effect=[0.0, 100.0]):
            summary = _run(
                st_client,
                image_client,
                [PUBLIC_ITEM, STORAGE_ITEM],
                ledger=ledger,
                deadline=50.0,
            )

        assert summary.uploaded == 1
        assert upload.calls[0].request.url.params["external_item_id"] == "200"

    @respx.mock
    def test_successive_bounded_runs_reach_the_whole_catalogue(
        self, st_settings, st_client, image_client
    ) -> None:
        """The property the whole design exists for: two one-asset runs cover
        two assets, rather than uploading the first one twice."""
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.get(_images_url(st_settings)).mock(return_value=httpx.Response(200, content=JPEG))
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        store = InMemorySheetsStore()
        records = [PUBLIC_ITEM, STORAGE_ITEM]

        for _ in range(2):
            ledger = ImageLedger(store)
            with patch("st_exporter.images.upload.monotonic", side_effect=[0.0, 100.0]):
                upload_pricebook_images(
                    st_client, image_client, ledger, records, now=NOW, deadline=50.0
                )
            ledger.flush()

        uploaded = {call.request.url.params["external_item_id"] for call in upload.calls}
        assert uploaded == {"100", "200"}

    @respx.mock
    def test_a_re_verified_asset_moves_behind_the_ones_it_was_ahead_of(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        store = InMemorySheetsStore()
        first = ImageLedger(store)
        _run(st_client, image_client, [PUBLIC_ITEM], ledger=first)
        first.flush()

        later = ImageLedger(store)
        summary = upload_pricebook_images(
            st_client, image_client, later, [PUBLIC_ITEM], now="2026-09-17T12:00:00+00:00"
        )

        assert summary.already_uploaded == 1
        assert later.last_verified("100:a1") == "2026-09-17T12:00:00+00:00"

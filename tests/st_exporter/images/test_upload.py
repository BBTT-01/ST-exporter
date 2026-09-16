"""Tests for the image upload pass: both identifier forms, replay, 403, failures.

Real ``ServiceTitanClient`` and real ``TrueQuoteImageClient`` throughout — only
the transport is faked (respx) and the Sheet is in memory, so what is exercised
is the actual download-and-POST path, not a mock of it.
"""

from __future__ import annotations

from typing import Generator
from unittest.mock import patch

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_exporter.images.client import TrueQuoteImageClient
from st_exporter.images.ledger import ImageLedger
from st_exporter.images.upload import (
    ASSET_CAP_REACHED,
    BUDGET_SPENT,
    upload_pricebook_images,
)
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


# ---------------------------------------------------------------------------
# BOUNDED AND RESUMABLE.
#
# Run 35130164187 on `BBTT-01/tr-doorservpro`: ~7,191 assets, the process
# SIGKILLed by the runner at 10m35s, 0 images uploaded. It could not converge on
# its own, because a killed process flushes no ledger and the next run started
# the catalogue from the top and was killed in the same place.
#
# Two things had to become true, and neither is sufficient alone: the pass must
# STOP ITSELF while it can still write the ledger, and the next pass must START
# WHERE THIS ONE STOPPED.
# ---------------------------------------------------------------------------


class _FakeClock:
    """A `monotonic()` that advances one second per reading.

    The pass reads the clock exactly once per asset, before starting it, so one
    tick per asset makes "a budget of N seconds" mean "N assets" — which is what
    lets these tests talk about how far a bounded run gets without sleeping.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        reading = self.now
        self.now += 1.0
        return reading


def _item(item_id: int) -> dict:
    return {
        "id": item_id,
        "modifiedOn": "2026-09-14T00:00:00+00:00",
        "assets": [{"id": f"a{item_id}", "url": f"https://cdn.example.com/{item_id}.jpg"}],
    }


def _bounded_run(st_client, image_client, records, store, *, budget: float) -> tuple:
    """One bounded pass over ``records`` against the ledger persisted in ``store``.

    Returns ``(summary, uploaded_item_ids)``. The ledger is re-read from the
    store each time and flushed at the end, exactly as a fresh process would —
    that is the part run 35130164187 never reached.
    """
    ledger = ImageLedger(store)
    uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
    before = len(uploads.calls)
    with patch("st_exporter.images.upload.monotonic", _FakeClock()):
        summary = upload_pricebook_images(
            st_client, image_client, ledger, records, now=NOW, deadline=budget
        )
    ledger.flush()
    sent = [call.request.url.params["external_item_id"] for call in list(uploads.calls)[before:]]
    return summary, sent


class TestTheDeadlineStopsItCleanly:
    @respx.mock
    def test_a_spent_budget_stops_the_pass_and_says_so(
        self, st_settings, st_client, image_client
    ) -> None:
        """`stopped` is set, and set BEFORE the process would have been killed.

        That is the whole difference: a pass that stops itself returns a summary
        and flushes a ledger, while a SIGKILLed one writes nothing at all and
        every upload it managed is re-sent by the next run, for ever.
        """
        mock_auth_token(st_settings.auth_url)
        for item_id in range(1, 7):
            respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        records = [_item(i) for i in range(1, 7)]

        summary, sent = _bounded_run(
            st_client, image_client, records, InMemorySheetsStore(), budget=3
        )

        assert summary.stopped == BUDGET_SPENT
        # Three assets started, three never reached — and the pass knows which.
        assert len(sent) == 3
        assert summary.pending == 3
        assert summary.considered == 3

    @respx.mock
    def test_no_budget_at_all_never_stops_early(self, st_settings, st_client, image_client) -> None:
        """A local run, and every existing test, passes no deadline. That must
        stay a pass that runs to the end of the catalogue."""
        mock_auth_token(st_settings.auth_url)
        for item_id in range(1, 6):
            respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run(st_client, image_client, [_item(i) for i in range(1, 6)])

        assert summary.stopped is None
        assert (summary.uploaded, summary.pending) == (5, 0)

    @respx.mock
    def test_the_ledger_survives_a_stopped_pass(self, st_settings, st_client, image_client) -> None:
        """The point of stopping cleanly, stated as an assertion: what the pass
        DID upload before it ran out of budget is still on disk afterwards."""
        mock_auth_token(st_settings.auth_url)
        for item_id in range(1, 7):
            respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        store = InMemorySheetsStore()

        _bounded_run(st_client, image_client, [_item(i) for i in range(1, 7)], store, budget=3)

        rows = store.tabs["_image_ledger"][1:]
        assert len(rows) == 3, "a stopped pass must still record what it delivered"


class TestASecondRunResumesRatherThanRestarting:
    @respx.mock
    def test_two_bounded_runs_sweep_a_catalogue_one_run_cannot(
        self, st_settings, st_client, image_client
    ) -> None:
        """THE EVIDENCE. Six assets, a budget that fits three, run twice.

        Run 2 must upload the three run 1 never reached — not the three it
        already did. The old pass re-tread the same prefix every time, which is
        why a 7,191-asset tenant uploaded zero images on every run for ever
        rather than converging over a few hours.
        """
        mock_auth_token(st_settings.auth_url)
        downloads = {
            item_id: respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
            for item_id in range(1, 7)
        }
        records = [_item(i) for i in range(1, 7)]
        store = InMemorySheetsStore()

        first, sent_first = _bounded_run(st_client, image_client, records, store, budget=3)
        downloaded_in_run_one = {i for i, route in downloads.items() if route.calls}

        second, sent_second = _bounded_run(st_client, image_client, records, store, budget=3)

        assert first.stopped == BUDGET_SPENT
        assert sorted(sent_first) == sorted(str(i) for i in downloaded_in_run_one)
        # Run 2 starts where run 1 stopped: disjoint from it, and between them
        # the two runs cover the whole catalogue.
        assert not set(sent_first) & set(sent_second), (
            f"run 2 re-trod run 1's prefix: {sent_first} then {sent_second}"
        )
        assert sorted(sent_first + sent_second) == sorted(str(i) for i in range(1, 7))
        # Run 2 spends its own three-asset budget on the three run 1 missed, so
        # it too reports `pending` — the three it already delivered. That is a
        # sweep converging, not a failure, and the run after it proves it: full
        # coverage, nothing left to visit, and nothing to upload.
        assert second.stopped == BUDGET_SPENT and second.pending == 3
        third, sent_third = _bounded_run(st_client, image_client, records, store, budget=100)
        assert (sent_third, third.pending, third.stopped) == ([], 0, None)

    @respx.mock
    def test_a_third_run_over_a_converged_catalogue_downloads_nothing(
        self, st_settings, st_client, image_client
    ) -> None:
        """Once swept, a pass costs no bytes at all.

        This is the pre-download half of the ledger check. The idempotency key
        hashes the PAYLOAD, so `ledger.has(key)` cannot be asked until the bytes
        are in hand — which is what used to force a re-download of all ~7,191
        assets on every run just to rediscover they had already been sent. The
        item's `modifiedOn` is the cheap validator that answers it earlier.
        """
        mock_auth_token(st_settings.auth_url)
        downloads = {
            item_id: respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
            for item_id in range(1, 7)
        }
        records = [_item(i) for i in range(1, 7)]
        store = InMemorySheetsStore()

        _bounded_run(st_client, image_client, records, store, budget=3)
        _bounded_run(st_client, image_client, records, store, budget=3)
        before = {i: len(route.calls) for i, route in downloads.items()}

        third, sent = _bounded_run(st_client, image_client, records, store, budget=100)

        assert sent == [], "a converged catalogue must upload nothing"
        assert {i: len(route.calls) for i, route in downloads.items()} == before, (
            "a converged catalogue must DOWNLOAD nothing either — that is the "
            "root inefficiency this check exists to remove"
        )
        assert third.revalidated == 6
        assert third.stopped is None and third.pending == 0

    @respx.mock
    def test_a_modified_item_is_downloaded_again(
        self, st_settings, st_client, image_client
    ) -> None:
        """The skip fails CLOSED. An item ServiceTitan says changed after our
        last confirmation is re-downloaded, so replaced bytes still reach
        TrueQuote."""
        mock_auth_token(st_settings.auth_url)
        route = respx.get("https://cdn.example.com/1.jpg").mock(
            return_value=httpx.Response(200, content=PNG)
        )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()

        _bounded_run(st_client, image_client, [_item(1)], store, budget=100)
        moved = _item(1) | {"modifiedOn": "2099-01-01T00:00:00+00:00"}
        summary, _ = _bounded_run(st_client, image_client, [moved], store, budget=100)

        assert len(route.calls) == 2, "a changed item must be re-read, not assumed"
        assert summary.revalidated == 0

    @respx.mock
    def test_an_item_with_no_modified_on_is_never_assumed_fresh(
        self, st_settings, st_client, image_client
    ) -> None:
        """Blank means "cannot prove freshness", never "fresh"."""
        mock_auth_token(st_settings.auth_url)
        route = respx.get("https://cdn.example.com/1.jpg").mock(
            return_value=httpx.Response(200, content=PNG)
        )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()
        undated = {"id": 1, "assets": [{"id": "a1", "url": "https://cdn.example.com/1.jpg"}]}

        _bounded_run(st_client, image_client, [undated], store, budget=100)
        summary, _ = _bounded_run(st_client, image_client, [undated], store, budget=100)

        assert len(route.calls) == 2
        assert summary.revalidated == 0
        assert summary.already_uploaded == 1, "still deduped, just not for free"

    @respx.mock
    def test_a_cheaply_skipped_asset_is_not_pruned_out_of_the_ledger(
        self, st_settings, st_client, image_client
    ) -> None:
        """The trap under the pre-download skip.

        `ImageLedger.keep` prunes every key this run did not SEE, and a skipped
        asset computes no content hash — so without deliberately re-contributing
        its known keys, a converged catalogue would prune itself empty and
        re-upload everything on the run after that.
        """
        mock_auth_token(st_settings.auth_url)
        respx.get("https://cdn.example.com/1.jpg").mock(
            return_value=httpx.Response(200, content=PNG)
        )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()

        _bounded_run(st_client, image_client, [_item(1)], store, budget=100)
        summary, _ = _bounded_run(st_client, image_client, [_item(1)], store, budget=100)

        ledger = ImageLedger(store)
        ledger.keep(summary.seen_keys)
        ledger.flush()
        assert len(store.tabs["_image_ledger"][1:]) == 1, (
            "the prune forgot an asset the pass deliberately did not download"
        )


# ---------------------------------------------------------------------------
# CONDITIONAL REQUESTS.
#
# The weekly re-verification used to be a full RE-DOWNLOAD of every asset,
# because the only cheap freshness signal — the ITEM's `modifiedOn` — is a proxy
# for an ASSET-level change that nobody has confirmed on a live tenant. The
# insurance stays; its price does not. A stored `ETag`/`Last-Modified` is quoted
# back, and a 304 is a verification that moved no bytes.
#
# Every test below also pins the FALLBACK, because nothing here is known to work
# against real ServiceTitan: no validator in the response means a plain GET and
# the behaviour the pass has always had.
# ---------------------------------------------------------------------------

LATER = "2026-09-24T12:00:00+00:00"  # NOW + 10 days: past REVERIFY_AFTER_DAYS.
ETAG = '"asset-v1"'
LAST_MODIFIED = "Mon, 14 Sep 2026 00:00:00 GMT"


def _run_at(st_client, image_client, records, store, when, **kwargs):
    """One pass over ``records`` at wall-clock ``when``, ledger persisted in ``store``."""
    ledger = ImageLedger(store)
    summary = upload_pricebook_images(st_client, image_client, ledger, records, now=when, **kwargs)
    ledger.flush()
    return summary


def _ledger_rows(store) -> list[list[str]]:
    return store.tabs["_image_ledger"][1:]


class TestConditionalRequestsReplaceTheWeeklyReDownload:
    @respx.mock
    def test_a_304_verifies_the_asset_without_downloading_or_uploading_it(
        self, st_settings, st_client, image_client
    ) -> None:
        """THE EVIDENCE for the 304 path.

        Run 1 downloads the bytes and stores the server's `ETag`. Ten days later
        — past `REVERIFY_AFTER_DAYS`, so the `modifiedOn` shortcut has expired
        and the old code would have re-downloaded the whole image — run 2 sends
        `If-None-Match`, gets a 304 with an EMPTY body, and is done.
        """
        mock_auth_token(st_settings.auth_url)
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()
        bytes_on_the_wire: list[int] = []

        def respond(request: httpx.Request) -> httpx.Response:
            if request.headers.get("if-none-match") == ETAG:
                bytes_on_the_wire.append(0)
                return httpx.Response(304, headers={"etag": ETAG})
            bytes_on_the_wire.append(len(PNG))
            return httpx.Response(200, content=PNG, headers={"etag": ETAG})

        route = respx.get("https://cdn.example.com/1.jpg").mock(side_effect=respond)

        first = _run_at(st_client, image_client, [_item(1)], store, NOW)
        second = _run_at(st_client, image_client, [_item(1)], store, LATER)

        assert (first.uploaded, first.not_modified) == (1, 0)
        # The re-verification happened, and cost NO bytes and NO upload.
        assert route.call_count == 2, "the weekly re-verification must still happen"
        assert (second.not_modified, second.uploaded) == (1, 0)
        assert second.already_uploaded == 1
        assert upload.call_count == 1, "a 304 must never re-POST bytes TrueQuote has"
        assert bytes_on_the_wire == [len(PNG), 0], (
            "the whole point: the second verification moved zero image bytes"
        )
        assert second.conditional.conditional_sent == 1
        assert second.conditional.not_modified == 1
        print("\n304 EVIDENCE:", second.conditional.as_log_fields())
        print("304 VERDICT :", second.conditional.verdict())
        print("304 SUMMARY :", second.as_log_fields())

    @respx.mock
    def test_a_304_refreshes_the_verification_timestamp(
        self, st_settings, st_client, image_client
    ) -> None:
        """A 304 that did not re-stamp the ledger would sort this asset to the
        front of every later pass for ever — the non-convergence the ordering
        exists to prevent, reintroduced through the cheapest door."""
        mock_auth_token(st_settings.auth_url)
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()
        respx.get("https://cdn.example.com/1.jpg").mock(
            side_effect=lambda request: (
                httpx.Response(304)
                if request.headers.get("if-none-match")
                else httpx.Response(200, content=PNG, headers={"etag": ETAG})
            )
        )

        _run_at(st_client, image_client, [_item(1)], store, NOW)
        assert [row[3] for row in _ledger_rows(store)] == [NOW]

        _run_at(st_client, image_client, [_item(1)], store, LATER)

        assert [row[3] for row in _ledger_rows(store)] == [LATER]

    @respx.mock
    def test_a_200_with_a_validator_uploads_and_stores_it(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(
            return_value=httpx.Response(
                200, content=PNG, headers={"etag": ETAG, "last-modified": LAST_MODIFIED}
            )
        )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()

        summary = _run_at(st_client, image_client, [PUBLIC_ITEM], store, NOW)

        assert summary.uploaded == 1
        row = _ledger_rows(store)[0]
        assert (row[4], row[5]) == (ETAG, LAST_MODIFIED)
        assert summary.conditional.validators_present == 1
        assert summary.conditional.validators_absent == 0

    @respx.mock
    def test_both_validators_are_quoted_back(self, st_settings, st_client, image_client) -> None:
        """We do not know which of the two (if either) ServiceTitan implements,
        so both are sent when both are known."""
        mock_auth_token(st_settings.auth_url)
        route = respx.get("https://cdn.example.com/1.jpg").mock(
            return_value=httpx.Response(
                200, content=PNG, headers={"etag": ETAG, "last-modified": LAST_MODIFIED}
            )
        )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()

        _run_at(st_client, image_client, [_item(1)], store, NOW)
        _run_at(st_client, image_client, [_item(1)], store, LATER)

        second = route.calls[1].request
        assert second.headers["if-none-match"] == ETAG
        assert second.headers["if-modified-since"] == LAST_MODIFIED

    @respx.mock
    def test_the_authenticated_endpoint_is_asked_conditionally_too(
        self, st_settings, st_client, image_client
    ) -> None:
        """ServiceTitan's own images endpoint is the one nobody has measured."""
        mock_auth_token(st_settings.auth_url)
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()
        images = respx.get(_images_url(st_settings)).mock(
            side_effect=lambda request: (
                httpx.Response(304)
                if request.headers.get("if-none-match")
                else httpx.Response(200, content=JPEG, headers={"etag": ETAG})
            )
        )

        _run_at(st_client, image_client, [STORAGE_ITEM], store, NOW)
        summary = _run_at(st_client, image_client, [STORAGE_ITEM], store, LATER)

        assert images.call_count == 2
        # The auth header still goes with it — the conditional headers are
        # merged OVER the auth ones, never instead of them.
        assert images.calls[1].request.headers["authorization"] == "Bearer test-token"
        assert images.calls[1].request.headers["if-none-match"] == ETAG
        assert summary.not_modified == 1


class TestItDegradesToTodaysBehaviourWithNoValidators:
    @respx.mock
    def test_a_response_with_no_validators_still_works_and_is_counted(
        self, st_settings, st_client, image_client
    ) -> None:
        """The defensive case, and the one that may well be reality.

        No `ETag`, no `Last-Modified`: nothing to quote back, so the weekly
        re-verification is a plain GET and a full download — exactly what the
        pass did before. It must still dedupe, and the run must SAY that the
        server offered nothing, because that is the measurement.
        """
        mock_auth_token(st_settings.auth_url)
        route = respx.get("https://cdn.example.com/1.jpg").mock(
            return_value=httpx.Response(200, content=PNG)
        )
        upload = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()

        first = _run_at(st_client, image_client, [_item(1)], store, NOW)
        second = _run_at(st_client, image_client, [_item(1)], store, LATER)

        assert first.uploaded == 1
        assert route.call_count == 2, "with no validator the re-check must re-download"
        assert not route.calls[1].request.headers.get("if-none-match")
        assert (second.uploaded, second.already_uploaded) == (0, 1)
        assert upload.call_count == 1, "identical bytes are still never re-sent"
        assert second.conditional.validators_absent == 1
        assert second.conditional.conditional_sent == 0
        assert "NO validator" in second.conditional.verdict()
        print("\nFALLBACK EVIDENCE:", second.conditional.as_log_fields())
        print("FALLBACK VERDICT :", second.conditional.verdict())

    @respx.mock
    def test_a_pre_upgrade_ledger_row_is_not_discarded(
        self, st_settings, st_client, image_client
    ) -> None:
        """The upgrade run must not re-upload a 7,000-image catalogue.

        Every ledger written before this feature has four columns, not six. A
        strict width check would drop every row as malformed.
        """
        mock_auth_token(st_settings.auth_url)
        route = respx.get("https://cdn.example.com/1.jpg").mock(
            return_value=httpx.Response(200, content=PNG)
        )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()

        _run_at(st_client, image_client, [_item(1)], store, NOW)
        # Rewrite the ledger in the OLD four-column shape.
        legacy = [list(row[:4]) for row in _ledger_rows(store)]
        store.replace_grid(
            "_image_ledger",
            [["idempotency_key", "asset_ref", "storage_path", "verified_at"], *legacy],
        )

        summary = _run_at(st_client, image_client, [_item(1)], store, NOW)

        assert route.call_count == 1, "the old row must still buy the free skip"
        assert summary.revalidated == 1

    @respx.mock
    def test_a_conditional_request_answered_200_is_counted_as_a_miss(
        self, st_settings, st_client, image_client
    ) -> None:
        """A server that IGNORES `If-None-Match` answers 200 with the whole body.

        That is the outcome the summary has to be able to name, because it is
        indistinguishable from "the image changed" without the counter.
        """
        mock_auth_token(st_settings.auth_url)
        respx.get("https://cdn.example.com/1.jpg").mock(
            return_value=httpx.Response(200, content=PNG, headers={"etag": ETAG})
        )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()

        _run_at(st_client, image_client, [_item(1)], store, NOW)
        summary = _run_at(st_client, image_client, [_item(1)], store, LATER)

        assert summary.conditional.conditional_sent == 1
        assert summary.conditional.not_modified == 0
        assert summary.conditional.conditional_missed == 1
        assert "NOT ONE 304" in summary.conditional.verdict()

    @respx.mock
    def test_a_signed_url_is_reported(self, st_settings, st_client, image_client) -> None:
        """Signed urls would explain a 0% hit rate, and cannot be fixed here."""
        mock_auth_token(st_settings.auth_url)
        signed = "https://cdn.example.com/1.jpg?X-Amz-Signature=deadbeef&X-Amz-Credential=k"
        respx.get(signed).mock(return_value=httpx.Response(200, content=PNG))
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        record = {"id": 1, "assets": [{"id": None, "url": signed}]}

        summary = _run_at(st_client, image_client, [record], InMemorySheetsStore(), NOW)

        assert summary.conditional.signed_urls == 1
        # No `asset.id`, so the ledger's own reference is the url: a fresh url
        # next listing is a fresh asset and nothing can ever match.
        assert summary.conditional.unstable_refs == 1
        assert "look signed" in summary.conditional.verdict()


class TestThePerRunAssetCap:
    @respx.mock
    def test_the_cap_stops_the_pass_with_its_own_reason(
        self, st_settings, st_client, image_client
    ) -> None:
        """Two stopping conditions, two reasons. An operator reading
        `images_stopped=` must know which dial to turn."""
        mock_auth_token(st_settings.auth_url)
        for item_id in range(1, 7):
            respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run_at(
            st_client,
            image_client,
            [_item(i) for i in range(1, 7)],
            InMemorySheetsStore(),
            NOW,
            max_assets=2,
        )

        assert summary.stopped == ASSET_CAP_REACHED
        assert summary.stopped != BUDGET_SPENT
        assert (summary.fetched, summary.uploaded, summary.pending) == (2, 2, 4)
        assert summary.complete is False, "a capped pass has not seen the catalogue"

    @respx.mock
    def test_no_cap_is_the_default_and_never_bites(
        self, st_settings, st_client, image_client
    ) -> None:
        """A default that truncated a catalogue for ever would be a trap."""
        mock_auth_token(st_settings.auth_url)
        for item_id in range(1, 7):
            respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run_at(
            st_client, image_client, [_item(i) for i in range(1, 7)], InMemorySheetsStore(), NOW
        )

        assert summary.stopped is None
        assert (summary.uploaded, summary.pending) == (6, 0)

    @respx.mock
    def test_the_cap_and_the_deadline_are_separate_conditions(
        self, st_settings, st_client, image_client
    ) -> None:
        """The same catalogue, the same cap, a tighter clock: the clock wins and
        says so. Neither reason may shadow the other."""
        mock_auth_token(st_settings.auth_url)
        for item_id in range(1, 7):
            respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        ledger = ImageLedger(InMemorySheetsStore())

        with patch("st_exporter.images.upload.monotonic", _FakeClock()):
            summary = upload_pricebook_images(
                st_client,
                image_client,
                ledger,
                [_item(i) for i in range(1, 7)],
                now=NOW,
                deadline=2,
                max_assets=4,
            )

        assert summary.stopped == BUDGET_SPENT
        assert summary.fetched == 2

    @respx.mock
    def test_a_free_skip_does_not_consume_the_cap(
        self, st_settings, st_client, image_client
    ) -> None:
        """The cap bounds WORK, not the scan.

        Counting `modifiedOn` skips would stop a converged catalogue part-way
        through a sweep it could have finished for nothing — and would make
        every run report `stopped`, so the ledger would never be pruned again.
        """
        mock_auth_token(st_settings.auth_url)
        for item_id in range(1, 7):
            respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()
        records = [_item(i) for i in range(1, 7)]

        _run_at(st_client, image_client, records, store, NOW)  # converge first
        summary = _run_at(st_client, image_client, records, store, NOW, max_assets=2)

        assert summary.revalidated == 6
        assert (summary.fetched, summary.stopped, summary.pending) == (0, None, 0)
        assert summary.complete is True

    @respx.mock
    def test_two_capped_runs_sweep_different_slices_and_the_union_converges(
        self, st_settings, st_client, image_client
    ) -> None:
        """THE EVIDENCE for the cap.

        Six assets, a cap of three, run twice against the same ledger. The cap
        has to compose with least-recently-verified-first ordering or it is
        simply a truncation: run 2 must fetch the three run 1 never reached.
        """
        mock_auth_token(st_settings.auth_url)
        downloads = {
            item_id: respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
            for item_id in range(1, 7)
        }
        uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        records = [_item(i) for i in range(1, 7)]
        store = InMemorySheetsStore()

        first = _run_at(st_client, image_client, records, store, NOW, max_assets=3)
        slice_a = {i for i, route in downloads.items() if route.calls}
        sent_a = [call.request.url.params["external_item_id"] for call in uploads.calls]

        second = _run_at(st_client, image_client, records, store, NOW, max_assets=3)
        slice_b = {i for i, route in downloads.items() if route.calls} - slice_a
        later_calls = list(uploads.calls)[len(sent_a) :]
        sent_b = [call.request.url.params["external_item_id"] for call in later_calls]

        assert first.stopped == ASSET_CAP_REACHED
        assert len(slice_a) == 3 and len(slice_b) == 3
        assert not slice_a & slice_b, f"run 2 re-trod run 1's slice: {slice_a} then {slice_b}"
        assert slice_a | slice_b == set(range(1, 7)), "the union must be the whole catalogue"
        assert not set(sent_a) & set(sent_b)
        assert sorted(sent_a + sent_b) == sorted(str(i) for i in range(1, 7))
        # Run 2 spends its own cap on the three run 1 never reached — they sort
        # FIRST, having never been verified — and stops on the cap with run 1's
        # three still to visit. That is a sweep converging, and the run after it
        # proves it: every asset now has a timestamp, every one is skipped for
        # free, the cap never bites and the pass sees the catalogue WHOLE.
        assert (second.fetched, second.stopped, second.pending) == (3, ASSET_CAP_REACHED, 3)
        third = _run_at(st_client, image_client, records, store, NOW, max_assets=3)
        assert (third.revalidated, third.fetched) == (6, 0)
        assert third.stopped is None and third.pending == 0 and third.complete is True
        print(f"\nCAP EVIDENCE: run1 fetched {sorted(slice_a)} stopped={first.stopped!r}")
        print(f"CAP EVIDENCE: run2 fetched {sorted(slice_b)} stopped={second.stopped!r}")
        print(
            f"CAP EVIDENCE: union={sorted(slice_a | slice_b)} overlap={sorted(slice_a & slice_b)}"
        )
        print(
            f"CAP EVIDENCE: run3 revalidated={third.revalidated} fetched={third.fetched} "
            f"stopped={third.stopped!r} complete={third.complete}"
        )


class TestTheSummaryLineAnswersWhyItStopped:
    """One log line, both halves: the cap in force and whether it bit.

    An operator reading `images_stopped=` should never have to cross-reference
    the workflow inputs to learn which dial to turn.
    """

    @respx.mock
    def test_the_cap_and_the_fact_it_bit_are_both_on_the_line(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        for item_id in range(1, 5):
            respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run_at(
            st_client,
            image_client,
            [_item(i) for i in range(1, 5)],
            InMemorySheetsStore(),
            NOW,
            max_assets=2,
        )

        line = summary.as_log_fields()
        assert "max_assets=2" in line
        assert "cap_hit=true" in line
        assert f"stopped={ASSET_CAP_REACHED}" in line
        print("\nCAP SUMMARY LINE:", line)

    @respx.mock
    def test_an_uncapped_run_says_so_rather_than_printing_a_zero(
        self, st_settings, st_client, image_client
    ) -> None:
        """`max_assets=0` would read as "cap of zero", which is the opposite."""
        mock_auth_token(st_settings.auth_url)
        respx.get(PUBLIC_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = _run_at(st_client, image_client, [PUBLIC_ITEM], InMemorySheetsStore(), NOW)

        assert "max_assets=none" in summary.as_log_fields()
        assert "cap_hit=false" in summary.as_log_fields()

    @respx.mock
    def test_a_budget_stop_is_not_reported_as_a_cap_hit(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        for item_id in range(1, 5):
            respx.get(f"https://cdn.example.com/{item_id}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        ledger = ImageLedger(InMemorySheetsStore())

        with patch("st_exporter.images.upload.monotonic", _FakeClock()):
            summary = upload_pricebook_images(
                st_client,
                image_client,
                ledger,
                [_item(i) for i in range(1, 5)],
                now=NOW,
                deadline=2,
                max_assets=1000,
            )

        assert summary.cap_hit is False
        assert f"stopped={BUDGET_SPENT}" in summary.as_log_fields()
        assert "max_assets=1000" in summary.as_log_fields()

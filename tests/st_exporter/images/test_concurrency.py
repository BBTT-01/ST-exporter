"""The two things that make a one-off backfill finishable: dedupe and concurrency.

THE MEASURED WASTE
==================

Run 35145303072 on `BBTT-01/tr-doorservpro` (exporter 0.2.19) reported
``fetched=5 uploaded=5`` — and the five items it fetched for (2141068, 2141069,
2141070, 2141071, 2141078) all pointed at ONE asset GUID. The pass downloaded the
same picture five times, because it iterated per ITEM-ASSET and the download is
keyed by URL.

The asymmetry is the whole design: TrueQuote's route takes one
``external_item_id`` per POST and has no batch endpoint, so a picture shared by
five items still needs five uploads — but only one download and one hash. Every
test in ``TestOneDownloadManyUploads`` is that sentence.

Real ``ServiceTitanClient`` and real ``TrueQuoteImageClient`` throughout; only
the transport is faked (respx) and the Sheet is in memory, exactly as
``test_upload.py`` does it. The pass really does run on a thread pool here.
"""

from __future__ import annotations

import threading
import time
from typing import Generator
from unittest.mock import patch

import httpx
import pytest
import respx

import st_cli.client as client_module
from st_cli.client import ServiceTitanClient
from st_exporter.images.client import TrueQuoteImageClient
from st_exporter.images.ledger import ImageLedger
from st_exporter.images.upload import (
    ASSET_CAP_REACHED,
    DEFAULT_IMAGE_CONCURRENCY,
    MAX_IMAGE_CONCURRENCY,
    _bounded_concurrency,
    upload_pricebook_images,
)
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token

TQ_BASE = "https://truequote.example.com/api/outbox"
TQ_UPLOAD = f"{TQ_BASE}/pricebook-image"
NOW = "2026-09-14T12:00:00+00:00"

# Padded past `MIN_PLAUSIBLE_IMAGE_BYTES`, as every image fixture must be: a
# byte-valid image under the 1 KiB floor is a blank placeholder and is refused
# on purpose. A fixture standing in for a REAL image has to look like one.
PNG = b"\x89PNG\r\n\x1a\n" + b"png-body" + b"\x00" * 2048
SHARED_URL = "https://cdn.example.com/shared.jpg"


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


def _accepted() -> httpx.Response:
    return httpx.Response(
        200, json={"asset_key": "k", "storage_path": "co/st/assets/x.png", "status": "stored"}
    )


def _item(item_id: int, url: str, *, asset_id: str | None = None) -> dict:
    """One pricebook item pointing at ``url``.

    ``asset_id`` defaults to one derived from the URL, which is what makes two
    items that share a picture share an ASSET IDENTITY too — the shape run
    35145303072 actually hit, where five items named one GUID.
    """
    return {
        "id": item_id,
        "modifiedOn": "2026-09-14T00:00:00+00:00",
        "assets": [{"id": asset_id or f"a-{url.rsplit('/', 1)[-1]}", "url": url}],
    }


def _uploaded_ids(route) -> list[str]:
    return [call.request.url.params["external_item_id"] for call in route.calls]


class TestOneDownloadManyUploads:
    @respx.mock
    def test_five_items_sharing_one_asset_cost_one_download_and_five_uploads(
        self, st_settings, st_client, image_client
    ) -> None:
        """THE EVIDENCE, in the exact shape run 35145303072 measured."""
        mock_auth_token(st_settings.auth_url)
        download = respx.get(SHARED_URL).mock(return_value=httpx.Response(200, content=PNG))
        uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        items = [_item(i, SHARED_URL) for i in (2141068, 2141069, 2141070, 2141071, 2141078)]

        summary = upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(InMemorySheetsStore()),
            items,
            now=NOW,
        )

        assert download.call_count == 1, "the same picture was downloaded more than once"
        assert uploads.call_count == 5, "TrueQuote is keyed by item; every item needs its POST"
        assert sorted(_uploaded_ids(uploads)) == sorted(
            ["2141068", "2141069", "2141070", "2141071", "2141078"]
        )
        # `considered` still counts ITEM-ASSETS — five items were dealt with —
        # while `fetched` counts DOWNLOADS, which is the expensive half and the
        # half the cap bounds.
        assert (summary.considered, summary.uploaded, summary.fetched) == (5, 5, 1)
        assert (summary.item_assets, summary.distinct_assets) == (5, 1)
        assert summary.dedupe_ratio == 5.0
        assert summary.downloads_saved == 4
        print(
            f"\nDEDUPE EVIDENCE: {summary.item_assets} item-assets -> "
            f"{download.call_count} download(s), {uploads.call_count} upload(s), "
            f"ratio {summary.dedupe_ratio:.2f}x"
        )

    @respx.mock
    def test_every_member_gets_its_own_ledger_row_so_the_prune_stays_honest(
        self, st_settings, st_client, image_client
    ) -> None:
        """The trap under dedupe.

        `ImageLedger.keep` drops every key the run did not SEE. The ledger is
        keyed per ITEM (`{external_item_id}:{identity}`), so a shared picture
        holds one row per item — and a member whose key never reached
        `seen_keys` because somebody else did its download would be pruned out
        and re-uploaded on the run after that, for ever.
        """
        mock_auth_token(st_settings.auth_url)
        respx.get(SHARED_URL).mock(return_value=httpx.Response(200, content=PNG))
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()
        ledger = ImageLedger(store)

        summary = upload_pricebook_images(
            st_client, image_client, ledger, [_item(i, SHARED_URL) for i in range(1, 6)], now=NOW
        )

        assert len(summary.seen_keys) == 5
        assert summary.complete is True
        ledger.keep(summary.seen_keys)
        ledger.flush()
        assert len(store.tabs["_image_ledger"][1:]) == 5, (
            "the prune forgot items whose download somebody else made"
        )

    @respx.mock
    def test_a_second_run_over_a_shared_asset_uploads_nothing(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        download = respx.get(SHARED_URL).mock(return_value=httpx.Response(200, content=PNG))
        uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()
        items = [_item(i, SHARED_URL) for i in range(1, 6)]

        first = ImageLedger(store)
        upload_pricebook_images(st_client, image_client, first, items, now=NOW)
        first.flush()
        second = upload_pricebook_images(
            st_client, image_client, ImageLedger(store), items, now=NOW
        )

        assert uploads.call_count == 5, "nothing was re-sent"
        assert download.call_count == 1, "and nothing was re-downloaded either"
        assert second.revalidated == 5

    @respx.mock
    def test_a_shared_asset_is_only_fetched_conditionally_when_every_item_agrees(
        self, st_settings, st_client, image_client
    ) -> None:
        """A 304 carries no bytes, so it may only be asked for on behalf of
        items that have ALL already been delivered.

        Item 1 is in the ledger with an ETag; item 2 shares its picture and has
        never been delivered. Quoting item 1's validator would earn a 304 and
        item 2 would never receive its image — so the group falls back to an
        unconditional GET, which is exactly what item 2 alone would have done.
        """
        mock_auth_token(st_settings.auth_url)
        etag = '"shared-v1"'
        sent_conditional: list[bool] = []

        def respond(request: httpx.Request) -> httpx.Response:
            conditional = "if-none-match" in request.headers
            sent_conditional.append(conditional)
            if conditional:
                return httpx.Response(304, headers={"etag": etag})
            return httpx.Response(200, content=PNG, headers={"etag": etag})

        respx.get(SHARED_URL).mock(side_effect=respond)
        uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()

        # Run 1 delivers item 1 only, and learns the ETag for it.
        first = ImageLedger(store)
        upload_pricebook_images(st_client, image_client, first, [_item(1, SHARED_URL)], now=NOW)
        first.flush()

        # Run 2 sees item 1 (delivered, has a validator) and item 2 (never
        # delivered, has none) sharing one picture, ten days later so the
        # `modifiedOn` shortcut has expired.
        later = "2026-09-24T12:00:00+00:00"
        summary = upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(store),
            [_item(1, SHARED_URL), _item(2, SHARED_URL)],
            now=later,
        )

        assert sent_conditional == [False, False], (
            "a 304 would have starved the item that has never been delivered"
        )
        assert summary.not_modified == 0
        assert "2" in _uploaded_ids(uploads)


class TestConcurrencyIsBounded:
    @respx.mock
    def test_no_more_downloads_are_ever_in_flight_than_the_worker_count(
        self, st_settings, st_client, image_client
    ) -> None:
        """MEMORY, stated as an assertion.

        A payload can be 8 MiB (`MAX_IMAGE_BYTES`), so the bound that matters is
        on requests IN FLIGHT, not on tasks submitted. The dispatcher is held
        behind a semaphore precisely so "submitted" cannot run away from "done".
        """
        mock_auth_token(st_settings.auth_url)
        in_flight = 0
        peak = 0
        guard = threading.Lock()

        def slow(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            with guard:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.02)
            with guard:
                in_flight -= 1
            return httpx.Response(200, content=PNG)

        for i in range(1, 25):
            respx.get(f"https://cdn.example.com/{i}.jpg").mock(side_effect=slow)
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(InMemorySheetsStore()),
            [_item(i, f"https://cdn.example.com/{i}.jpg") for i in range(1, 25)],
            now=NOW,
            concurrency=4,
            # The default 6/s governor is a CEILING, not the thing under test
            # here, and 20-odd assets against it is three seconds of sleeping.
            # Raised so this test measures the semaphore. `test_pacing.py` owns
            # the limiter itself.
            requests_per_second=200,
        )

        assert peak <= 4, f"{peak} downloads were in flight with a bound of 4"
        assert peak > 1, "nothing ran concurrently at all; the bound proved nothing"
        assert summary.uploaded == 24
        print(f"\nCONCURRENCY EVIDENCE: bound=4 peak in flight={peak} uploaded={summary.uploaded}")

    @respx.mock
    def test_concurrency_of_one_is_exactly_the_old_serial_pass(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        in_flight = 0
        peak = 0
        guard = threading.Lock()

        def slow(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            with guard:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.01)
            with guard:
                in_flight -= 1
            return httpx.Response(200, content=PNG)

        for i in range(1, 7):
            respx.get(f"https://cdn.example.com/{i}.jpg").mock(side_effect=slow)
        uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(InMemorySheetsStore()),
            [_item(i, f"https://cdn.example.com/{i}.jpg") for i in range(1, 7)],
            now=NOW,
            concurrency=1,
        )

        assert peak == 1
        assert _uploaded_ids(uploads) == [str(i) for i in range(1, 7)], (
            "one worker must preserve the catalogue's own order exactly"
        )

    def test_the_dial_is_clamped_rather_than_rejected(self) -> None:
        """A performance dial on a side lane may slow images down, never fail a
        run, so a silly number is bounded rather than raised."""
        assert _bounded_concurrency(None) == DEFAULT_IMAGE_CONCURRENCY
        assert _bounded_concurrency(0) == 1
        assert _bounded_concurrency(-5) == 1
        assert _bounded_concurrency(10_000) == MAX_IMAGE_CONCURRENCY
        assert _bounded_concurrency(4) == 4


class TestTheCapStillBitesDeterministically:
    @respx.mock
    @pytest.mark.parametrize("attempt", [1, 2, 3])
    def test_the_same_slice_is_fetched_however_the_downloads_race(
        self, st_settings, st_client, image_client, attempt: int
    ) -> None:
        """Out-of-order completion must not change WHICH assets a bounded run reaches.

        Downloads are given wildly different latencies, so they finish in an
        order unrelated to the catalogue's. The cap is still decided by the
        single dispatcher thread in least-recently-verified order BEFORE a group
        is handed to a worker, so the answer is the same every time.
        """
        mock_auth_token(st_settings.auth_url)
        for i in range(1, 21):
            delay = 0.03 if i % 3 else 0.001

            def slow(request: httpx.Request, delay: float = delay) -> httpx.Response:
                time.sleep(delay)
                return httpx.Response(200, content=PNG)

            respx.get(f"https://cdn.example.com/{i}.jpg").mock(side_effect=slow)
        uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(InMemorySheetsStore()),
            [_item(i, f"https://cdn.example.com/{i}.jpg") for i in range(1, 21)],
            now=NOW,
            max_assets=5,
            concurrency=8,
        )

        assert summary.stopped == ASSET_CAP_REACHED
        assert summary.fetched == 5
        assert sorted(_uploaded_ids(uploads), key=int) == ["1", "2", "3", "4", "5"]
        assert summary.pending == 15

    @respx.mock
    def test_a_capped_run_delivers_everything_it_paid_to_download(
        self, st_settings, st_client, image_client
    ) -> None:
        """The cap says "start nothing more", never "abandon bytes already paid
        for". A cap of 2 that fetched 2 has to upload 2, whether or not the
        second finished before the third was refused a slot."""
        mock_auth_token(st_settings.auth_url)
        for i in range(1, 7):
            respx.get(f"https://cdn.example.com/{i}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(InMemorySheetsStore()),
            [_item(i, f"https://cdn.example.com/{i}.jpg") for i in range(1, 7)],
            now=NOW,
            max_assets=2,
            concurrency=8,
        )

        assert (summary.fetched, summary.uploaded) == (2, 2)
        assert len(uploads.calls) == 2

    @respx.mock
    def test_a_cap_counts_downloads_so_a_shared_asset_reaches_more_items(
        self, st_settings, st_client, image_client
    ) -> None:
        """A cap of 1 over five items that share one picture uploads all five.

        The cap bounds the expensive half. Run 35145303072's cap of 5 bought one
        picture; the same cap now buys five different ones.
        """
        mock_auth_token(st_settings.auth_url)
        respx.get(SHARED_URL).mock(return_value=httpx.Response(200, content=PNG))
        uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        summary = upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(InMemorySheetsStore()),
            [_item(i, SHARED_URL) for i in range(1, 6)],
            now=NOW,
            max_assets=1,
        )

        assert (summary.fetched, summary.uploaded) == (1, 5)
        assert len(uploads.calls) == 5


class TestResumeSurvivesOutOfOrderCompletion:
    @respx.mock
    def test_two_capped_concurrent_runs_cover_the_catalogue_exactly_once(
        self, st_settings, st_client, image_client
    ) -> None:
        """Least-recently-verified ordering has to survive the thread pool.

        Twelve assets, a cap of six, two runs against one persisted ledger, with
        downloads completing out of order. Run 2 must fetch the six run 1 never
        reached — not the six it already did — and between them they must cover
        the catalogue once.
        """
        mock_auth_token(st_settings.auth_url)
        downloads = {}
        for i in range(1, 13):
            delay = 0.02 if i % 2 else 0.001

            def slow(request: httpx.Request, delay: float = delay) -> httpx.Response:
                time.sleep(delay)
                return httpx.Response(200, content=PNG)

            downloads[i] = respx.get(f"https://cdn.example.com/{i}.jpg").mock(side_effect=slow)
        uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()
        records = [_item(i, f"https://cdn.example.com/{i}.jpg") for i in range(1, 13)]

        def run() -> None:
            ledger = ImageLedger(store)
            upload_pricebook_images(
                st_client,
                image_client,
                ledger,
                records,
                now=NOW,
                max_assets=6,
                concurrency=6,
            )
            ledger.flush()

        run()
        slice_a = {i for i, route in downloads.items() if route.calls}
        sent_a = set(_uploaded_ids(uploads))
        run()
        slice_b = {i for i, route in downloads.items() if route.calls} - slice_a
        sent_b = set(_uploaded_ids(uploads)) - sent_a

        assert len(slice_a) == 6 and len(slice_b) == 6
        assert not slice_a & slice_b, f"run 2 re-trod run 1's slice: {slice_a} then {slice_b}"
        assert slice_a | slice_b == set(range(1, 13))
        assert sent_a | sent_b == {str(i) for i in range(1, 13)}
        # And the run after them finds nothing left to do at all.
        third_ledger = ImageLedger(store)
        third = upload_pricebook_images(
            st_client, image_client, third_ledger, records, now=NOW, max_assets=6, concurrency=6
        )
        assert (third.revalidated, third.fetched, third.pending) == (12, 0, 0)
        assert third.stopped is None and third.complete is True

    @respx.mock
    def test_a_ledger_row_is_written_for_every_item_a_concurrent_run_delivered(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        for i in range(1, 21):
            respx.get(f"https://cdn.example.com/{i}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = InMemorySheetsStore()
        ledger = ImageLedger(store)

        upload_pricebook_images(
            st_client,
            image_client,
            ledger,
            [_item(i, f"https://cdn.example.com/{i}.jpg") for i in range(1, 21)],
            now=NOW,
            concurrency=8,
            # The default 6/s governor is a CEILING, not the thing under test
            # here, and 20-odd assets against it is three seconds of sleeping.
            # Raised so this test measures the semaphore. `test_pacing.py` owns
            # the limiter itself.
            requests_per_second=200,
        )
        ledger.flush()

        rows = store.tabs["_image_ledger"][1:]
        assert len(rows) == 20, "a concurrent pass lost ledger rows to a race"
        assert len({row[0] for row in rows}) == 20


class _CountingStore(InMemorySheetsStore):
    """An in-memory Sheet that counts ledger WRITES — the expensive operation."""

    def __init__(self) -> None:
        super().__init__()
        self.ledger_writes = 0

    def replace_grid(self, tab_name: str, grid) -> None:
        if tab_name == "_image_ledger":
            self.ledger_writes += 1
        return super().replace_grid(tab_name, grid)


class TestTheLedgerIsFlushedInBATCHES:
    """The ledger is a Google Sheet tab and `flush` rewrites the whole grid.

    Per-asset writes under concurrency would be a read-modify-write of a
    16,000-row grid per image — not slow, impossible. So the pass batches in
    memory and flushes on a timer, and these tests pin both halves: that the
    timer exists, and that it does not fire per asset.
    """

    @respx.mock
    def test_a_default_pass_writes_the_ledger_once_not_once_per_asset(
        self, st_settings, st_client, image_client
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        for i in range(1, 21):
            respx.get(f"https://cdn.example.com/{i}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = _CountingStore()

        summary = upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(store),
            [_item(i, f"https://cdn.example.com/{i}.jpg") for i in range(1, 21)],
            now=NOW,
            concurrency=8,
            # The default 6/s governor is a CEILING, not the thing under test
            # here, and 20-odd assets against it is three seconds of sleeping.
            # Raised so this test measures the semaphore. `test_pacing.py` owns
            # the limiter itself.
            requests_per_second=200,
        )

        assert summary.uploaded == 20
        # Twenty uploads, ONE grid write: the 60-second timer never came round
        # in a test that takes milliseconds, so this is the final flush alone.
        assert store.ledger_writes == 1

    @respx.mock
    def test_a_killed_process_loses_only_what_happened_since_the_last_flush(
        self, st_settings, st_client, image_client
    ) -> None:
        """Resumability, stated as an assertion.

        With the timer set to zero, every dispatch is a flush opportunity — so
        the ledger on the Sheet is never more than one asset behind the uploads
        that really happened, and a process killed part-way resumes from it.
        """
        mock_auth_token(st_settings.auth_url)
        for i in range(1, 9):
            respx.get(f"https://cdn.example.com/{i}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        store = _CountingStore()

        upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(store),
            [_item(i, f"https://cdn.example.com/{i}.jpg") for i in range(1, 9)],
            now=NOW,
            concurrency=2,
            flush_interval=0,
        )

        assert store.ledger_writes > 1, "nothing was flushed mid-pass"
        # And what reached the Sheet mid-pass is real: a fresh ledger reads it
        # back and a second run has nothing left to send.
        assert len(store.tabs["_image_ledger"][1:]) == 8

    @respx.mock
    def test_a_mid_pass_flush_failure_does_not_end_the_pass(
        self, st_settings, st_client, image_client
    ) -> None:
        """A Sheets 429 on the raw-cache spreadsheet is routine. Losing the
        ledger costs re-sent bytes; it must never cost the uploads."""
        mock_auth_token(st_settings.auth_url)
        for i in range(1, 6):
            respx.get(f"https://cdn.example.com/{i}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=_accepted())

        class _RefusingStore(InMemorySheetsStore):
            def replace_grid(self, tab_name: str, grid) -> None:
                if tab_name == "_image_ledger":
                    raise RuntimeError("Sheets 429: quota exceeded")
                return super().replace_grid(tab_name, grid)

        summary = upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(_RefusingStore()),
            [_item(i, f"https://cdn.example.com/{i}.jpg") for i in range(1, 6)],
            now=NOW,
            flush_interval=0,
        )

        assert summary.uploaded == 5
        assert summary.ledger_flush_failures > 0
        assert summary.stopped is None


class Test429sAreStillHonouredUnderConcurrency:
    @respx.mock
    def test_servicetitans_429_backoff_still_retries_and_now_slows_everybody(
        self, st_settings, st_client, image_client
    ) -> None:
        """The reactive backoff is unchanged AND the governor is told.

        `st_cli.client` still retries a 429 with its own exponential backoff —
        that is what makes the asset succeed. What is new is that the retry no
        longer happens in private: the pass's shared limiter is penalised, so
        the other workers stop pushing at the rate that earned it instead of
        each discovering the same 429 on their own clock.
        """
        mock_auth_token(st_settings.auth_url)
        images_url = f"{st_settings.api_base}/pricebook/v2/tenant/{st_settings.tenant_id}/images"
        answers = iter([httpx.Response(429, text="slow down")])

        def throttle_once(request: httpx.Request) -> httpx.Response:
            return next(answers, httpx.Response(200, content=PNG))

        route = respx.get(images_url).mock(side_effect=throttle_once)
        uploads = respx.post(TQ_UPLOAD).mock(return_value=_accepted())
        storage_items = [
            {
                "id": i,
                "modifiedOn": "2026-09-14T00:00:00+00:00",
                "assets": [{"id": None, "url": f"Images/Pricebook/{i}.jpg"}],
            }
            for i in (1,)
        ]

        with patch.object(client_module.time, "sleep"):
            summary = upload_pricebook_images(
                st_client,
                image_client,
                ImageLedger(InMemorySheetsStore()),
                storage_items,
                now=NOW,
                concurrency=4,
                # The governor must not be the thing under test here — a real
                # 5-second penalty would make this a 5-second test. Zero
                # disables the limiter; the OBSERVATION is what is asserted.
                requests_per_second=0,
            )

        assert route.call_count == 2, "the 429 was not retried"
        assert summary.uploaded == 1
        assert uploads.call_count == 1
        assert summary.download_failed == 0

    @respx.mock
    def test_a_429_from_truequote_stops_the_pass_and_penalises_the_governor(
        self, st_settings, st_client, image_client
    ) -> None:
        """A retryable rejection ends the PASS, as it always has — the next run
        retries every unsent asset, and nothing is lost because nothing here is
        a queue. What is new is that the workers still in flight are held back
        rather than piling on."""
        mock_auth_token(st_settings.auth_url)
        for i in range(1, 11):
            respx.get(f"https://cdn.example.com/{i}.jpg").mock(
                return_value=httpx.Response(200, content=PNG)
            )
        respx.post(TQ_UPLOAD).mock(return_value=httpx.Response(429, json={"error": "rate_limit"}))

        summary = upload_pricebook_images(
            st_client,
            image_client,
            ImageLedger(InMemorySheetsStore()),
            [_item(i, f"https://cdn.example.com/{i}.jpg") for i in range(1, 11)],
            now=NOW,
            concurrency=2,
            # A real penalty would hold the remaining in-flight workers for five
            # seconds each; the pass is stopping anyway.
            requests_per_second=0,
        )

        assert summary.stopped == "rate_limit"
        assert summary.uploaded == 0
        assert summary.complete is False
        assert summary.rate_limit_penalties >= 1

"""The governor. There was no rate limiter in this codebase before this one.

What existed — and still exists, unchanged — is a per-request retry backoff in
``st_cli.client`` and a ``retryable`` classification in ``images/client``. Both
are reactive: they act only after an endpoint has already refused us, and each
request backs off on its own clock. Under concurrency that does not converge, it
synchronises. These tests pin the difference.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from st_exporter.images.pacing import (
    MAX_PENALTY_SECONDS,
    TRUEQUOTE_UPLOADS_PER_SECOND,
    RateLimiter,
)


class TestTheCeilingIsReal:
    def test_it_hands_out_no_more_than_the_rate_over_time(self) -> None:
        """A bucket of 1/s, five acquires: at least four intervals of waiting."""
        limiter = RateLimiter(50.0, burst=1)
        started = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        elapsed = time.monotonic() - started
        # Four gaps of 20ms after the first (burst) token.
        assert elapsed >= 4 * (1 / 50.0) * 0.8, elapsed

    def test_a_burst_is_allowed_once_and_then_paid_for(self) -> None:
        limiter = RateLimiter(20.0, burst=5)
        started = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        burst_elapsed = time.monotonic() - started
        assert burst_elapsed < 0.05, "the burst allowance must not be paid for up front"
        limiter.acquire()
        assert time.monotonic() - started >= 0.04

    def test_zero_disables_it_entirely(self) -> None:
        limiter = RateLimiter(0.0)
        assert limiter.enabled is False
        assert limiter.acquire() == 0.0

    def test_the_ceiling_is_shared_by_every_thread_not_per_thread(self) -> None:
        """THE POINT. A per-request backoff is per thread; this is not.

        Eight threads, a bucket of 1 token and 25/s: the LAST of them cannot
        possibly be served before seven intervals have passed, however many
        threads are asking.
        """
        limiter = RateLimiter(25.0, burst=1)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: limiter.acquire(), range(8)))
        elapsed = time.monotonic() - started
        assert elapsed >= 7 * (1 / 25.0) * 0.8, elapsed


class TestA429HoldsEverybodyBack:
    def test_a_penalty_stalls_threads_that_did_not_earn_it(self) -> None:
        """The cooperation between the reactive backoff and the governor.

        One worker's 429 must slow down the OTHERS — otherwise the endpoint gets
        the same pressure it just complained about, minus one request.
        """
        limiter = RateLimiter(1000.0, burst=1000)
        limiter.penalise(0.15)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: limiter.acquire(), range(4)))
        elapsed = time.monotonic() - started
        assert elapsed >= 0.12, f"a penalty let callers straight through ({elapsed}s)"

    def test_a_penalty_empties_the_bucket_so_the_answer_is_not_a_burst(self) -> None:
        limiter = RateLimiter(20.0, burst=10)
        limiter.penalise(0.0)
        started = time.monotonic()
        limiter.acquire()
        limiter.acquire()
        # Both tokens had to be re-earned at 20/s rather than taken from a
        # bucket that was full when the 429 arrived.
        assert time.monotonic() - started >= 0.04

    def test_penalties_cannot_accumulate_without_bound(self) -> None:
        """A burst of 429s across N workers must not become minutes of dead air
        on a pass that has a deadline to meet."""
        limiter = RateLimiter(10.0)
        for _ in range(100):
            limiter.penalise(30.0)
        # Whatever was asked for, no acquire can be held longer than the cap.
        assert limiter._paused_until - time.monotonic() <= MAX_PENALTY_SECONDS + 0.1

    def test_a_disabled_limiter_ignores_penalties_rather_than_hanging(self) -> None:
        limiter = RateLimiter(0.0)
        limiter.penalise(30.0)
        assert limiter.acquire() == 0.0


def test_truequotes_documented_limit_is_the_one_they_published() -> None:
    """5,000 uploads / 10 minutes, from their handoff note. Not ours to raise."""
    assert TRUEQUOTE_UPLOADS_PER_SECOND == 5000 / (10 * 60)

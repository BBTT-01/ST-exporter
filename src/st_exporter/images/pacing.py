"""A real, shared, client-side ceiling on how fast the image pass may issue requests.

WHY THIS HAD TO BE WRITTEN RATHER THAN CONFIGURED
=================================================

There was no rate limiter in this codebase. What existed — and still exists — is
purely REACTIVE: ``st_cli.client`` sleeps ``_BACKOFF_BASE * 2 ** retries`` after
a 429 and tries again, and ``images/client`` classes 429 as retryable. Neither
is a governor. Both only ever act AFTER the endpoint has already told us we went
too fast, and a backoff is per-request state: N workers that each hit a 429 each
sleep on their own clock, wake up together, and hit it again. Independent
exponential backoff under concurrency does not converge, it SYNCHRONISES — the
classic thundering herd — and the pass discovers a limit by collecting 429s,
which for TrueQuote ends the pass outright.

So the pass gets a proper governor, and the reactive backoff is made to
cooperate with it instead of fighting it (see ``penalise``).

TWO BUCKETS, NOT ONE
====================

The pass talks to two unrelated services with unrelated limits, so it holds one
limiter per side:

- **ServiceTitan** (the authenticated ``pricebook/.../images`` endpoint). Its
  real per-tenant ceiling is not documented to us and is believed to be the
  binding constraint in practice. Conservative, configurable, measured only by
  never hitting it.
- **TrueQuote** (``POST /pricebook-image``). Documented by them at 5,000 uploads
  per 10 minutes — 8.33/s — in their handoff note. Not implemented on their side
  as far as we can see, and not something to find out by experiment: a 429 from
  TrueQuote is ``retryable``, and a retryable rejection ENDS THE PASS.
  ``TRUEQUOTE_UPLOADS_PER_SECOND`` is therefore a hard clamp the configuration
  can lower but never raise.

A CEILING IS NOT A TARGET
=========================

At the default concurrency the pass cannot come near either rate — a download
plus an upload is seconds of wall clock, so eight workers produce fewer than two
of each per second. The limiter exists for the case where that stops being true:
a fast CDN, a raised concurrency dial, a tenant whose images are tiny. It costs
nothing when it is not binding, which is exactly what one wants from a governor.

Deliberately its own module, and deliberately reading ``time.monotonic``
directly: ``upload.py``'s ``monotonic`` name is the pass's DEADLINE clock, which
tests replace with a counter advancing one second per reading. Pacing must never
consume readings from that clock, or adding a throttle would silently move where
a time budget bites.
"""

from __future__ import annotations

import time
from threading import Condition

# TrueQuote's documented sustained ceiling: 5,000 uploads / 10 minutes. Their
# number, from their handoff note, not a measurement of ours. Treated as a hard
# clamp rather than a default, because the cost of exceeding it is a 429 and a
# 429 from TrueQuote ends the pass.
TRUEQUOTE_UPLOADS_PER_SECOND = 5000 / (10 * 60)  # 8.33/s

# What one 429 buys, when the limiter is TOLD about one. Deliberately much
# longer than the reactive per-request backoff (1s, 2s, 4s): this one pauses
# EVERY worker, so it is the pass conceding that its whole rate was wrong, not
# one request retrying. It is also why the herd cannot re-form — every thread
# resumes against the same shared schedule rather than against its own timer.
PENALTY_SECONDS = 5.0

# How much a penalty is allowed to accumulate. Without a ceiling, a burst of
# 429s across N workers would multiply into minutes of dead air on a pass that
# has a deadline to meet.
MAX_PENALTY_SECONDS = 60.0


class RateLimiter:
    """A token bucket shared by every worker in the pass.

    ``per_second`` is the sustained ceiling and ``burst`` the number of requests
    that may go out back to back after an idle period (default: one second's
    worth, minimum 1). ``per_second <= 0`` disables the limiter entirely, which
    is what a caller measuring something else asks for.

    Thread-safe, and FAIR in the only sense that matters here: waiters are woken
    against one shared schedule, so they cannot synchronise into a wave the way
    independent backoff timers do.
    """

    def __init__(self, per_second: float, *, burst: int | None = None) -> None:
        self._per_second = max(0.0, per_second)
        capacity = burst if burst is not None else max(1, int(per_second))
        self._capacity = float(max(1, capacity))
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._paused_until = 0.0
        self._cond = Condition()
        self.waited_seconds = 0.0
        self.penalties = 0

    @property
    def enabled(self) -> bool:
        return self._per_second > 0.0

    @property
    def per_second(self) -> float:
        """The ceiling in force, for the one log line that states it."""
        return self._per_second

    def acquire(self) -> float:
        """Block until this caller may send one request. Returns seconds waited.

        Never returns while a penalty is in force, whoever imposed it: that is
        the whole point of the penalty being on the LIMITER rather than in the
        request that earned it.
        """
        if not self.enabled:
            return 0.0
        started = time.monotonic()
        with self._cond:
            while True:
                now = time.monotonic()
                if now < self._paused_until:
                    self._cond.wait(timeout=self._paused_until - now)
                    continue
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    break
                # Exactly how long until the next whole token exists. Sleeping
                # for that rather than for a fixed slice is what keeps the
                # limiter from becoming its own source of jitter.
                self._cond.wait(timeout=(1.0 - self._tokens) / self._per_second)
        waited = time.monotonic() - started
        with self._cond:
            self.waited_seconds += waited
        return waited

    def penalise(self, seconds: float = PENALTY_SECONDS) -> None:
        """A 429 happened. Hold EVERY worker back, and empty the bucket.

        This is how the reactive backoff already in ``st_cli.client`` and
        ``images/client`` cooperates with the governor instead of fighting it.
        The retry itself still happens where it always did — nothing here
        re-sends anything — but while it is happening no other worker is
        allowed to keep pushing at the rate that just earned the 429.

        Emptying the bucket matters as much as the pause: a limiter that
        resumed with a full burst allowance would answer a rate complaint with
        a burst.
        """
        if not self.enabled:
            return
        with self._cond:
            now = time.monotonic()
            capped = min(seconds, MAX_PENALTY_SECONDS)
            self._paused_until = min(
                max(self._paused_until, now + capped), now + MAX_PENALTY_SECONDS
            )
            self._tokens = 0.0
            self._updated = now
            self.penalties += 1
            self._cond.notify_all()

    def _refill(self, now: float) -> None:
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._updated = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._per_second)

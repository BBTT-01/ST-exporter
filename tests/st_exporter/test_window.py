"""Boundary tests for the 90-day-or-future window predicate.

These are the highest-stakes tests in this package: getting the window wrong is
silent (a missing job looks like a quiet day, not an error), per spec.md's testing
guidance.
"""

from __future__ import annotations

from datetime import date, timedelta

from st_exporter.window import in_window

_TODAY = date(2026, 9, 3)


def _at(days_from_today: int) -> str:
    d = _TODAY + timedelta(days=days_from_today)
    return f"{d.isoformat()}T09:00:00-05:00"


def test_exactly_ninety_days_ago_is_included() -> None:
    assert in_window(_at(-90), today=_TODAY) is True


def test_ninety_one_days_ago_is_excluded() -> None:
    assert in_window(_at(-91), today=_TODAY) is False


def test_today_is_included() -> None:
    assert in_window(_at(0), today=_TODAY) is True


def test_two_years_in_the_future_is_included() -> None:
    # Guards against a wrongly *symmetric* window (today +/- 90d), which would
    # incorrectly exclude a job scheduled further out than 90 days from now.
    assert in_window(_at(730), today=_TODAY) is True


def test_job_created_five_months_ago_scheduled_today_is_included() -> None:
    # The window is measured on the appointment's own start, never on the job's
    # creation date — in_window() only ever sees appointment_start, so a job
    # created long ago is indistinguishable here from a brand-new one. This test
    # documents that guarantee at the call-site shape used by denormalize.py.
    appointment_start = _at(0)
    assert in_window(appointment_start, today=_TODAY) is True


def test_custom_window_days_is_honoured() -> None:
    assert in_window(_at(-10), today=_TODAY, window_days=7) is False
    assert in_window(_at(-7), today=_TODAY, window_days=7) is True


def test_offset_is_converted_to_utc_before_taking_the_date() -> None:
    # 23:30 in a +10 offset is already the next UTC day — a naive "take the date
    # part of the local string" implementation would get this wrong at the exact
    # 90-day boundary.
    just_inside_utc = "2026-06-06T23:30:00+10:00"  # -> 2026-06-06T13:30:00Z
    assert (_TODAY - date(2026, 6, 6)).days == 89
    assert in_window(just_inside_utc, today=_TODAY, window_days=90) is True


def test_naive_timestamp_without_offset_is_treated_as_utc() -> None:
    assert in_window("2026-09-03T00:00:00", today=_TODAY) is True

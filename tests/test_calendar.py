"""The calendar: the DST seams, the 13:00 close, and the year it refuses to guess.

The failure this module exists to prevent is quiet. A 0DTE contract on 2025-11-28 does not
expire at 16:00 ET, it expires at 13:00 ET; a trade timestamped 01:30 UTC belongs to the
previous New York day; and a March session opens at 13:30 UTC, not 14:30. Each of those is
one hour or three hours of exposure that no other module can see, because every other
module asks this one what time it is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from scorpion_mff.calendar import (
    KNOWN_YEARS,
    CalendarError,
    check_tables,
    dst_bounds,
    et_date,
    et_datetime,
    et_offset,
    et_offset_at,
    expires_at_close,
    is_trading_day,
    next_session,
    previous_session,
    report,
    session_for,
    session_of,
    sessions_between,
    trading_days,
)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ── the DST rule ─────────────────────────────────────────────────────────────
def test_dst_bounds_are_the_second_sunday_and_the_first_sunday():
    assert dst_bounds(2025) == (date(2025, 3, 9), date(2025, 11, 2))
    assert dst_bounds(2026) == (date(2026, 3, 8), date(2026, 11, 1))


def test_the_offset_follows_the_date_not_the_month():
    assert et_offset(date(2025, 3, 8)) == timedelta(hours=-5)     # Saturday, still EST
    assert et_offset(date(2025, 3, 9)) == timedelta(hours=-4)     # DST starts that morning
    assert et_offset(date(2025, 11, 1)) == timedelta(hours=-4)    # Saturday, still EDT
    assert et_offset(date(2025, 11, 2)) == timedelta(hours=-5)    # falls back that morning


def test_a_march_session_opens_at_1330_utc_and_a_february_one_at_1430():
    friday = session_for(date(2025, 3, 7))
    monday = session_for(date(2025, 3, 10))
    assert friday is not None and monday is not None
    assert (friday.open_utc, friday.close_utc) == (utc(2025, 3, 7, 14, 30), utc(2025, 3, 7, 21))
    assert (monday.open_utc, monday.close_utc) == (utc(2025, 3, 10, 13, 30), utc(2025, 3, 10, 20))


def test_a_november_session_flips_back_after_the_first_sunday():
    friday = session_for(date(2025, 10, 31))
    monday = session_for(date(2025, 11, 3))
    assert friday is not None and monday is not None
    assert friday.open_utc == utc(2025, 10, 31, 13, 30)
    assert monday.open_utc == utc(2025, 11, 3, 14, 30)
    assert monday.close_utc == utc(2025, 11, 3, 21)


def test_2026_dst_sessions_match_2025():
    early = session_for(date(2026, 3, 6))
    late = session_for(date(2026, 3, 9))
    fall = session_for(date(2026, 11, 2))
    assert early is not None and late is not None and fall is not None
    assert early.open_utc == utc(2026, 3, 6, 14, 30)
    assert late.open_utc == utc(2026, 3, 9, 13, 30)
    assert fall.open_utc == utc(2026, 11, 2, 14, 30)


def test_the_spring_seam_is_read_from_the_clock():
    before = et_datetime(utc(2025, 3, 9, 6, 59))
    after = et_datetime(utc(2025, 3, 9, 7, 0))
    assert (before.hour, before.minute) == (1, 59)
    assert before.utcoffset() == timedelta(hours=-5)
    assert (after.hour, after.minute) == (3, 0)                  # 02:00 never happens
    assert after.utcoffset() == timedelta(hours=-4)


def test_the_fall_seam_repeats_an_hour_without_repeating_a_date():
    first = et_datetime(utc(2025, 11, 2, 5, 59))                 # 01:59 EDT
    second = et_datetime(utc(2025, 11, 2, 6, 0))                 # 01:00 EST, same wall clock
    assert (first.hour, first.utcoffset()) == (1, timedelta(hours=-4))
    assert (second.hour, second.utcoffset()) == (1, timedelta(hours=-5))
    assert first.date() == second.date() == date(2025, 11, 2)


def test_an_instant_before_the_spring_transition_belongs_to_the_previous_date():
    # 04:30 UTC on the DST-start Sunday is still 23:30 Saturday in New York. A per-date
    # offset (-4 for 2025-03-09) says Sunday, an hour late.
    assert et_date(utc(2025, 3, 9, 4, 30)) == date(2025, 3, 8)
    assert et_offset(date(2025, 3, 9)) == timedelta(hours=-4)    # the session's offset
    assert et_offset_at(utc(2025, 3, 9, 4, 30)) == timedelta(hours=-5)


# ── the session ──────────────────────────────────────────────────────────────
def test_a_regular_session_is_390_minutes():
    session = session_for(date(2025, 9, 8))
    assert session is not None
    assert session.minutes == 390
    assert session.label == "regular"
    assert not session.half_day


def test_the_half_day_close_is_1300_et_not_1600():
    session = session_for(date(2025, 7, 3))
    assert session is not None
    assert session.half_day and session.label == "half"
    assert session.close_utc == utc(2025, 7, 3, 17)              # 13:00 EDT
    assert et_datetime(session.close_utc).hour == 13
    assert session.minutes == 210                                # not 390


def test_a_winter_half_day_closes_at_1800_utc():
    session = session_for(date(2025, 11, 28))
    assert session is not None
    assert session.close_utc == utc(2025, 11, 28, 18)            # 13:00 EST
    assert session.minutes == 210
    december = session_for(date(2025, 12, 24))
    assert december is not None and december.half_day
    assert december.close_utc == utc(2025, 12, 24, 18)


def test_contains_is_inclusive_at_both_edges():
    session = session_for(date(2025, 9, 8))
    assert session is not None
    assert session.contains(session.open_utc)
    assert session.contains(session.close_utc)
    assert not session.contains(session.open_utc - timedelta(seconds=1))
    assert not session.contains(session.close_utc + timedelta(seconds=1))


def test_minutes_to_close_is_signed_not_clamped():
    session = session_for(date(2025, 9, 8))
    assert session is not None
    assert session.minutes_to_close(utc(2025, 9, 8, 19, 45)) == 15.0
    assert session.minutes_to_close(utc(2025, 9, 8, 20, 0)) == 0.0
    assert session.minutes_to_close(utc(2025, 9, 8, 20, 1)) < 0


def test_the_cutoff_is_derived_from_this_sessions_close():
    regular = session_for(date(2025, 9, 8))
    half = session_for(date(2025, 11, 28))
    assert regular is not None and half is not None
    assert regular.cutoff_utc(15) == utc(2025, 9, 8, 19, 45)      # 15:45 ET
    assert half.cutoff_utc(15) == utc(2025, 11, 28, 17, 45)       # 12:45 ET, not 15:45


def test_session_of_resolves_the_date_then_the_clock_does_the_rest():
    evening = session_of(utc(2025, 9, 8, 23, 0))                 # 19:00 ET, after the bell
    assert evening is not None and evening.day == date(2025, 9, 8)
    assert not evening.contains(utc(2025, 9, 8, 23, 0))
    found = session_of(utc(2025, 9, 8, 14, 0))
    assert found is not None and found.contains(utc(2025, 9, 8, 14, 0))


def test_a_naive_datetime_is_refused():
    with pytest.raises(CalendarError):
        et_date(datetime(2025, 9, 8, 14, 0))


# ── holidays and half-days ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "day",
    [date(2025, 1, 1), date(2025, 4, 18), date(2025, 7, 4), date(2025, 11, 27),
     date(2025, 12, 25), date(2026, 1, 1), date(2026, 4, 3), date(2026, 6, 19),
     date(2026, 7, 3), date(2026, 12, 25)],
)
def test_holidays_have_no_session(day):
    assert not is_trading_day(day)
    assert session_for(day) is None


def test_weekends_have_no_session():
    assert session_for(date(2025, 9, 6)) is None
    assert session_for(date(2025, 9, 7)) is None
    assert session_for(date(2026, 11, 1)) is None


def test_the_day_after_thanksgiving_is_a_half_day():
    session = session_for(date(2026, 11, 27))
    assert session is not None and session.half_day
    assert session.close_utc == utc(2026, 11, 27, 18)


def test_expires_at_close_is_the_session_close_not_a_default_1600():
    assert expires_at_close(date(2025, 9, 8)) == utc(2025, 9, 8, 20)
    assert expires_at_close(date(2025, 7, 3)) == utc(2025, 7, 3, 17)
    assert expires_at_close(date(2026, 11, 27)) == utc(2026, 11, 27, 18)


def test_expires_at_close_refuses_a_day_without_a_session():
    with pytest.raises(CalendarError):
        expires_at_close(date(2025, 7, 4))
    with pytest.raises(CalendarError):
        expires_at_close(date(2024, 12, 31))


# ── the ET trading date ──────────────────────────────────────────────────────
def test_the_et_date_is_not_the_utc_date():
    moment = utc(2025, 9, 9, 1, 30)                              # 21:30 ET, Monday
    assert moment.date() == date(2025, 9, 9)
    assert et_date(moment) == date(2025, 9, 8)


def test_a_sunday_night_post_belongs_to_the_weekend_not_to_monday():
    # 02:00 UTC Monday is 22:00 ET Sunday: the UTC date says "a session is open", the ET
    # date says "the market has been shut for three hours".
    moment = utc(2025, 9, 8, 2, 0)
    assert moment.date() == date(2025, 9, 8)
    assert session_for(date(2025, 9, 8)) is not None
    assert session_of(moment) is None


def test_et_date_holds_in_both_halves_of_the_year():
    assert et_date(utc(2025, 1, 15, 12, 0)) == date(2025, 1, 15)
    assert et_date(utc(2025, 7, 15, 2, 0)) == date(2025, 7, 14)  # 22:00 EDT


def test_et_datetime_carries_the_offset_it_used():
    winter = et_datetime(utc(2025, 1, 15, 15, 0))
    summer = et_datetime(utc(2025, 7, 15, 15, 0))
    assert winter.hour == 10                                     # 15:00 UTC, EST
    assert summer.hour == 11                                     # 15:00 UTC, EDT
    assert winter.tzinfo == timezone(timedelta(hours=-5))
    assert summer.tzinfo == timezone(timedelta(hours=-4))


# ── the year it refuses to guess ─────────────────────────────────────────────
@pytest.mark.parametrize("year", [2024, 2027, 2030])
def test_an_unknown_year_raises_instead_of_assuming_a_session(year):
    with pytest.raises(CalendarError):
        session_for(date(year, 6, 2))


def test_an_unknown_year_raises_even_for_a_weekend():
    # A Saturday in 2027 is provably closed, but the calendar still refuses a partial
    # answer: the next caller's question might be a Tuesday.
    with pytest.raises(CalendarError):
        is_trading_day(date(2027, 1, 2))


def test_an_unknown_year_is_reported_before_the_tables_are_loaded():
    assert KNOWN_YEARS == (2025, 2026)


def test_a_range_that_reaches_an_unknown_year_raises_at_call_time():
    with pytest.raises(CalendarError):
        sessions_between(date(2024, 12, 30), date(2025, 1, 6))
    with pytest.raises(CalendarError):
        sessions_between(date(2026, 12, 28), date(2027, 1, 8))


def test_a_lookup_before_2025_raises():
    with pytest.raises(CalendarError):
        previous_session(date(2025, 1, 2))
    with pytest.raises(CalendarError):
        next_session(date(2026, 12, 31))


# ── window walking ───────────────────────────────────────────────────────────
def test_sessions_between_skips_the_weekend():
    days = trading_days(date(2025, 9, 5), date(2025, 9, 9))
    assert days == (date(2025, 9, 5), date(2025, 9, 8), date(2025, 9, 9))


def test_sessions_between_an_empty_window_is_empty():
    assert list(sessions_between(date(2025, 9, 10), date(2025, 9, 9))) == []


def test_next_and_previous_session_step_over_holidays_and_weekends():
    after_july_fourth = next_session(date(2025, 7, 3))
    assert after_july_fourth.day == date(2025, 7, 7)
    assert previous_session(date(2025, 9, 8)).day == date(2025, 9, 5)
    assert next_session(date(2026, 12, 24)).day == date(2026, 12, 28)


def test_report_counts_half_days_holidays_and_minutes():
    window = report(date(2025, 12, 22), date(2025, 12, 31))
    assert window["sessions"] == 7
    assert window["half_days"] == ["2025-12-24"]
    assert window["holidays"] == ["2025-12-25"]
    assert window["total_minutes"] == 6 * 390 + 210
    first = window["first"]
    assert isinstance(first, dict)
    assert first["day"] == "2025-12-22"
    last = window["last"]
    assert isinstance(last, dict)
    assert last["day"] == "2025-12-31"


def test_report_refuses_an_empty_window():
    with pytest.raises(CalendarError):
        report(date(2025, 9, 10), date(2025, 9, 9))


# ── table integrity ──────────────────────────────────────────────────────────
def test_the_tables_satisfy_their_own_invariants():
    assert check_tables() == ()


def test_every_known_year_covers_a_full_year_of_sessions():
    for year in KNOWN_YEARS:
        count = len(trading_days(date(year, 1, 1), date(year, 12, 31)))
        assert 248 <= count <= 252, f"{year} has {count} sessions"

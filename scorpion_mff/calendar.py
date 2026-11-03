"""The market calendar: the ET clock, the session, and the year it refuses to guess.

Expiry semantics live on top of this module. `0DTE` means "expires at today's close", and
on a half-day that close is 13:00 ET, not 16:00 - a difference that quietly turns a
same-day contract into an overnight hold at exactly the minute the market stops showing a
bid. The ET date is also the *trading date*: at 01:30 UTC a 2025-09-09 contract is still
Monday evening in New York, and a UTC day comparison closes it a day early.

Covered here, deliberately not a full exchange calendar:

  * the regular session (09:30-16:00 ET) and the published half-days (13:00 close);
  * the holidays that close US equities, for 2025 and 2026 only;
  * the US DST rule - second Sunday in March to the first Sunday in November - DERIVED
    from the date rather than assumed, because a March or November trade is otherwise
    silently an hour wrong;
  * the exact UTC instants of the DST transitions, so an instant inside the 2 a.m. seam is
    dated by the clock that was actually in force, not by a per-date approximation.

It FAILS CLOSED on any year it does not know: every lookup raises `CalendarError` rather
than assume a session. A guessed close is a guessed expiry, and a guessed expiry is how a
0DTE becomes an assignment. `check_tables()` re-derives the invariants the tables must
satisfy, so a typo in a holiday fails a test instead of a Tuesday.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone

OPEN_ET = time(9, 30)
CLOSE_ET = time(16, 0)
HALF_DAY_CLOSE_ET = time(13, 0)

# DST moves at 02:00 local: 07:00 UTC in March (still EST before), 06:00 UTC in November
# (still EDT before). Reading the instant instead of the date is what dates the seam right.
DST_START_UTC = time(7, 0)
DST_END_UTC = time(6, 0)

# A bounded search for the next/previous session: a week of holidays plus a weekend is 4.
MAX_SEARCH_DAYS = 10

HOLIDAYS_2025: frozenset[date] = frozenset({
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
})
HALF_DAYS_2025: frozenset[date] = frozenset({
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
})
HOLIDAYS_2026: frozenset[date] = frozenset({
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
})
HALF_DAYS_2026: frozenset[date] = frozenset({date(2026, 11, 27), date(2026, 12, 24)})

_CALENDAR: dict[int, tuple[frozenset[date], frozenset[date]]] = {
    2025: (HOLIDAYS_2025, HALF_DAYS_2025),
    2026: (HOLIDAYS_2026, HALF_DAYS_2026),
}
KNOWN_YEARS: tuple[int, ...] = tuple(sorted(_CALENDAR))


class CalendarError(ValueError):
    """A date the calendar cannot speak to. Never guessed."""


def _require_aware(moment: datetime) -> datetime:
    """A naive datetime has no market: assuming UTC shifts every join by hours."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise CalendarError(
            f"naive datetime {moment.isoformat()} has no timezone; refusing to guess"
        )
    return moment.astimezone(UTC)


def _require_known_year(day: date) -> None:
    if day.year not in _CALENDAR:
        raise CalendarError(f"no calendar for {day.year}; refusing to guess ({day.isoformat()})")


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def dst_bounds(year: int) -> tuple[date, date]:
    """US DST: second Sunday in March to the first Sunday in November."""
    return _nth_weekday(year, 3, 6, 2), _nth_weekday(year, 11, 6, 1)


def et_offset_at(moment: datetime) -> timedelta:
    """The exact UTC offset of Eastern time at an instant, seam included.

    DST starts at 07:00 UTC on the second Sunday of March and ends at 06:00 UTC on the
    first Sunday of November. Comparing the instant (not the date) means an hour before
    the spring-forward is still -5 and an hour after the fall-back is -5 too - which is
    what the clock said, and therefore what the market meant.

    The UTC year is the ET year for this purpose: DST is never in force at a year
    boundary, so the two only differ in months that share an offset.
    """
    utc = _require_aware(moment)
    start, end = dst_bounds(utc.year)
    begins = datetime.combine(start, DST_START_UTC, tzinfo=UTC)
    ends = datetime.combine(end, DST_END_UTC, tzinfo=UTC)
    return timedelta(hours=-4) if begins <= utc < ends else timedelta(hours=-5)


def et_offset(day: date) -> timedelta:
    """The offset in force while a date's session trades: -4 in DST, -5 otherwise.

    This is the per-date form, and it is exact for every instant the market is open: the
    spring transition is complete hours before 09:30, and on the fall-back date the open
    is already on standard time. Use `et_offset_at` to date an arbitrary instant.
    """
    start, end = dst_bounds(day.year)
    return timedelta(hours=-4) if start <= day < end else timedelta(hours=-5)


def et_datetime(moment: datetime) -> datetime:
    """An instant as a fixed-offset Eastern datetime: wall clock in ET, tzinfo its offset."""
    utc = _require_aware(moment)
    offset = et_offset_at(utc)
    return (utc + offset).replace(tzinfo=timezone(offset))


def et_date(moment: datetime) -> date:
    """The Eastern-time calendar date of an instant - the date the contract trades on."""
    return et_datetime(moment).date()


@dataclass(frozen=True, slots=True)
class Session:
    """One trading day. `close_utc` is when a same-day contract stops existing."""

    day: date
    open_utc: datetime
    close_utc: datetime
    half_day: bool = False
    label: str = "regular"

    @property
    def minutes(self) -> int:
        return int((self.close_utc - self.open_utc).total_seconds() // 60)

    def contains(self, moment: datetime) -> bool:
        """Whether an instant is inside the session. Both edges inclusive."""
        return self.open_utc <= _require_aware(moment) <= self.close_utc

    def minutes_to_close(self, moment: datetime) -> float:
        """Minutes until the close: positive before it, negative after. Never clamped."""
        return (self.close_utc - _require_aware(moment)).total_seconds() / 60.0

    def cutoff_utc(self, minutes: int) -> datetime:
        """The instant a same-day contract enters its flatten window.

        Derived from this session's close, so a half-day window starts at 12:45 ET and a
        regular one at 15:45 ET without a constant anywhere else claiming otherwise.
        """
        return self.close_utc - timedelta(minutes=minutes)

    def to_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "open_utc": self.open_utc.isoformat(),
            "close_utc": self.close_utc.isoformat(),
            "minutes": self.minutes,
            "half_day": self.half_day,
            "label": self.label,
        }


def is_trading_day(day: date) -> bool:
    """Whether US equities trade on a date. Raises for a year the tables do not cover.

    The year is checked before the weekend: an unknown year gets no partial answers, so a
    Saturday in 2027 raises rather than quietly returning False as if it were known.
    """
    _require_known_year(day)
    if day.weekday() >= 5:
        return False
    holidays, _ = _CALENDAR[day.year]
    return day not in holidays


def session_for(day: date) -> Session | None:
    """The session for a date, or None when the market is closed."""
    if not is_trading_day(day):
        return None
    _, half_days = _CALENDAR[day.year]
    offset = et_offset(day)
    half_day = day in half_days
    close_et = HALF_DAY_CLOSE_ET if half_day else CLOSE_ET
    return Session(
        day=day,
        open_utc=datetime.combine(day, OPEN_ET, tzinfo=UTC) - offset,
        close_utc=datetime.combine(day, close_et, tzinfo=UTC) - offset,
        half_day=half_day,
        label="half" if half_day else "regular",
    )


def session_of(moment: datetime) -> Session | None:
    """The session for an instant's ET date, or None when the market is closed that day.

    The clock is not consulted: an instant at 19:00 ET on a Tuesday maps to Tuesday's
    session, and `Session.contains` is what says the session is over. Resolving the date and
    testing the clock are separate on purpose - expiry needs exactly that split.
    """
    return session_for(et_date(moment))


def next_session(day: date) -> Session:
    """The first session strictly after `day`, or CalendarError if the search hits a wall."""
    for step in range(1, MAX_SEARCH_DAYS + 1):
        candidate = day + timedelta(days=step)
        _require_known_year(candidate)
        session = session_for(candidate)
        if session is not None:
            return session
    raise CalendarError(f"no session within {MAX_SEARCH_DAYS} days after {day.isoformat()}")


def previous_session(day: date) -> Session:
    """The last session strictly before `day`, or CalendarError if the search hits a wall."""
    for step in range(1, MAX_SEARCH_DAYS + 1):
        candidate = day - timedelta(days=step)
        _require_known_year(candidate)
        session = session_for(candidate)
        if session is not None:
            return session
    raise CalendarError(f"no session within {MAX_SEARCH_DAYS} days before {day.isoformat()}")


def expires_at_close(day: date) -> datetime:
    """The UTC instant a contract dated `day` stops existing. Never a default close."""
    session = session_for(day)
    if session is None:
        raise CalendarError(f"{day.isoformat()} has no session, so nothing expires at its close")
    return session.close_utc


def _walk_sessions(start: date, end: date) -> Iterator[Session]:
    day = start
    while day <= end:
        session = session_for(day)
        if session is not None:
            yield session
        day += timedelta(days=1)


def sessions_between(start: date, end: date) -> Iterator[Session]:
    """Sessions in `[start, end]`. Validates both ends at CALL time, not on first next().

    A range that reaches into an unknown year raises instead of truncating: a backtest that
    silently stops at 2026-12-31 looks complete and is not.
    """
    _require_known_year(start)
    _require_known_year(end)
    return _walk_sessions(start, end)


def trading_days(start: date, end: date) -> tuple[date, ...]:
    return tuple(session.day for session in sessions_between(start, end))


def report(start: date, end: date) -> dict[str, object]:
    """A window's sessions: counts, half-days, closed weekdays, and tradable minutes."""
    if end < start:
        raise CalendarError(f"empty window {start.isoformat()}..{end.isoformat()}")
    _require_known_year(start)
    _require_known_year(end)
    sessions = list(sessions_between(start, end))
    holidays: list[str] = []
    day = start
    while day <= end:
        if day.weekday() < 5 and not is_trading_day(day):
            holidays.append(day.isoformat())
        day += timedelta(days=1)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sessions": len(sessions),
        "half_days": [s.day.isoformat() for s in sessions if s.half_day],
        "holidays": holidays,
        "total_minutes": sum(s.minutes for s in sessions),
        "first": sessions[0].to_dict() if sessions else None,
        "last": sessions[-1].to_dict() if sessions else None,
    }


def check_tables() -> tuple[str, ...]:
    """Re-derive the invariants the tables must satisfy. Empty means clean.

    A calendar is data, and data drifts. This is the assertion a test can make once and
    that a future edit to the tables cannot quietly break: every holiday and half-day is
    a weekday, filed under its own year, and no date is both.
    """
    problems: list[str] = []
    for year, (holidays, half_days) in sorted(_CALENDAR.items()):
        for day in sorted(holidays):
            if day.year != year:
                problems.append(f"{day.isoformat()} filed under {year}")
            if day.weekday() >= 5:
                problems.append(f"holiday {day.isoformat()} is a weekend")
        for day in sorted(half_days):
            if day.year != year:
                problems.append(f"{day.isoformat()} filed under {year}")
            if day.weekday() >= 5:
                problems.append(f"half-day {day.isoformat()} is a weekend")
            if day in holidays:
                problems.append(f"{day.isoformat()} is both a holiday and a half-day")
    return tuple(problems)


__all__ = [
    "CLOSE_ET",
    "HALF_DAY_CLOSE_ET",
    "KNOWN_YEARS",
    "OPEN_ET",
    "CalendarError",
    "Session",
    "check_tables",
    "dst_bounds",
    "et_date",
    "et_datetime",
    "et_offset",
    "et_offset_at",
    "expires_at_close",
    "is_trading_day",
    "next_session",
    "previous_session",
    "report",
    "session_for",
    "session_of",
    "sessions_between",
    "trading_days",
]

"""The market calendar: sessions, half-days, holidays, and the DST rule that moves them.

Expiry semantics live on top of this module. `0DTE` means "expires at today's close", and on a
half-day the close is 18:00 UTC, not 21:00 - a difference that silently turns a same-day contract
into an overnight hold. The day roll that produced the replay defect is also a calendar event, so
having one definition of "what day is it, and when does it end" is the substrate both the reducer
and the verifier stand on.

Deliberately not a full exchange calendar. It covers the regular session, the published half-days,
the holidays that close US equities, and the US DST rule, and it FAILS CLOSED on any year it does
not know: `session_for` raises rather than assuming a full day.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

OPEN_ET = time(9, 30)
CLOSE_ET = time(16, 0)
HALF_DAY_CLOSE_ET = time(13, 0)

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


class CalendarError(ValueError):
    """A date the calendar cannot speak to. Never guessed."""


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def dst_bounds(year: int) -> tuple[date, date]:
    """US DST: second Sunday in March to the first Sunday in November."""
    return _nth_weekday(year, 3, 6, 2), _nth_weekday(year, 11, 6, 1)


def et_offset(day: date) -> timedelta:
    """UTC offset for Eastern time on a date: -4 in DST, -5 otherwise.

    The corpus's `time_et` column was Pacific, and every naive offset shifted the joins by hours.
    This is the one place that assumption lives, and it is DERIVED from the date rather than
    assumed, so a March or November trade is not silently an hour wrong.
    """
    start, end = dst_bounds(day.year)
    return timedelta(hours=-4) if start <= day < end else timedelta(hours=-5)


@dataclass(frozen=True, slots=True)
class Session:
    day: date
    open_utc: datetime
    close_utc: datetime
    half_day: bool = False
    label: str = "regular"

    @property
    def minutes(self) -> int:
        return int((self.close_utc - self.open_utc).total_seconds() // 60)

    def contains(self, moment: datetime) -> bool:
        return self.open_utc <= moment.astimezone(UTC) <= self.close_utc

    def to_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(), "open_utc": self.open_utc.isoformat(),
            "close_utc": self.close_utc.isoformat(), "minutes": self.minutes,
            "half_day": self.half_day, "label": self.label,
        }


def is_trading_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    if day.year not in _CALENDAR:
        raise CalendarError(f"no calendar for {day.year}; refusing to guess")
    holidays, _ = _CALENDAR[day.year]
    return day not in holidays


def session_for(day: date) -> Session | None:
    """The session for a date, or None when the market is closed."""
    if not is_trading_day(day):
        return None
    _, half_days = _CALENDAR[day.year]
    offset = et_offset(day)
    close_et = HALF_DAY_CLOSE_ET if day in half_days else CLOSE_ET
    return Session(
        day=day,
        open_utc=datetime.combine(day, OPEN_ET, tzinfo=UTC) - offset,
        close_utc=datetime.combine(day, close_et, tzinfo=UTC) - offset,
        half_day=day in half_days,
        label="half" if day in half_days else "regular",
    )


def et_date(moment: datetime) -> date:
    """The Eastern-time calendar date of an instant."""
    moment = moment.astimezone(UTC)
    return (moment + et_offset(moment.date())).date()


def session_of_safe(moment: datetime) -> Session | None:
    """The session containing an instant, or None. Raises only for unknown years."""
    return session_for(et_date(moment))


def sessions_between(start: date, end: date) -> Iterator[Session]:
    day = start
    while day <= end:
        try:
            session = session_for(day)
        except CalendarError:
            return
        if session is not None:
            yield session
        day += timedelta(days=1)


def trading_days(start: date, end: date) -> tuple[date, ...]:
    return tuple(s.day for s in sessions_between(start, end))


def expires_at_close(day: date) -> datetime:
    """The instant a same-day contract stops existing."""
    session = session_for(day)
    if session is None:
        raise CalendarError(f"{day.isoformat()} is not a trading day")
    return session.close_utc


def report(start: date, end: date) -> dict[str, object]:
    days = list(sessions_between(start, end))
    return {
        "start": start.isoformat(), "end": end.isoformat(), "sessions": len(days),
        "half_days": [d.day.isoformat() for d in days if d.half_day],
        "first": days[0].to_dict() if days else None,
        "last": days[-1].to_dict() if days else None,
        "total_minutes": sum(d.minutes for d in days),
    }

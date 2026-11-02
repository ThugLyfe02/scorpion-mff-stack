"""XNYS regular-session calendar for the paper service (holidays and 13:00 early closes).

Uses ``exchange_calendars`` when installed; otherwise a reviewed static table that must be extended
each December (``preflight`` fails closed for a year the table does not cover).
"""

from __future__ import annotations

from datetime import date, datetime, time

from .config import MARKET_TZ

_HOLIDAYS: dict[int, frozenset[date]] = {
    2025: frozenset(
        date(2025, m, d)
        for m, d in ((1, 1), (1, 9), (1, 20), (2, 17), (4, 18), (5, 26), (6, 19), (7, 4),
                     (9, 1), (11, 27), (12, 25))
    ),
    2026: frozenset(
        date(2026, m, d)
        for m, d in ((1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3), (9, 7),
                     (11, 26), (12, 25))
    ),
    2027: frozenset(
        date(2027, m, d)
        for m, d in ((1, 1), (1, 18), (2, 15), (3, 26), (5, 31), (6, 18), (7, 5), (9, 6),
                     (11, 25), (12, 24))
    ),
}  # fmt: skip
_EARLY_CLOSE: frozenset[date] = frozenset(
    {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24), date(2026, 11, 27),
     date(2026, 12, 24), date(2027, 11, 26)}
)  # fmt: skip
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


class CalendarNotCovered(LookupError):
    pass


def session_hours(day: date) -> tuple[datetime, datetime] | None:
    """Regular-session open/close (ET-aware) or None when XNYS is closed that day."""

    try:
        import exchange_calendars as xcals
    except ImportError:
        xcals = None
    if xcals is not None:  # pragma: no cover - optional dependency
        calendar = xcals.get_calendar("XNYS")
        if not calendar.is_session(day.isoformat()):
            return None
        open_ts = calendar.session_open(day.isoformat()).tz_convert(MARKET_TZ).to_pydatetime()
        close_ts = calendar.session_close(day.isoformat()).tz_convert(MARKET_TZ).to_pydatetime()
        return open_ts, close_ts
    if day.year not in _HOLIDAYS:
        raise CalendarNotCovered(f"no XNYS holiday table for {day.year}")
    if day.weekday() >= 5 or day in _HOLIDAYS[day.year]:
        return None
    close = EARLY_CLOSE if day in _EARLY_CLOSE else REGULAR_CLOSE
    return (
        datetime.combine(day, REGULAR_OPEN, tzinfo=MARKET_TZ),
        datetime.combine(day, close, tzinfo=MARKET_TZ),
    )

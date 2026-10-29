"""Calendar and coverage: the refusals that keep every measurement honest."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scorpion.bars import Bar, BarSet
from scorpion.coverage import assess, classify, spans_sessions
from scorpion.sessions import (
    CalendarError,
    et_offset,
    expires_at_close,
    is_trading_day,
    report,
    session_for,
    trading_days,
)

D = Decimal
DAY = date(2026, 9, 1)


def barset(prices, *, day=DAY, ticker="QQQ", side="PUT", strike="710", start_hour=13,
           minute_step=5, sessions=1):
    bars = []
    for i, price in enumerate(prices):
        ts = datetime(day.year, day.month, day.day, start_hour, 30, tzinfo=UTC) + timedelta(
            minutes=minute_step * i
        )
        bars.append(Bar(ts_utc=ts, open=D(str(price)), high=D(str(price)),
                        low=D(str(price)), close=D(str(price))))
    return BarSet(ticker=ticker, side=side, strike=D(strike), day=day, bars=tuple(bars))


def trade(*, at=None, exit_pct=50, exit_price=None, day=DAY):
    return SimpleNamespace(
        identity=f"chan|{day}|QQQ|PUT|710|09:31", channel="etf-options", ticker="QQQ",
        side="PUT", strike=D("710"), day=day,
        ts_utc=at or datetime(day.year, day.month, day.day, 13, 31, tzinfo=UTC),
        exit_price=D(str(exit_price)) if exit_price is not None else None,
        lifecycle=SimpleNamespace(exit_pct=exit_pct),
    )


# ── calendar ────────────────────────────────────────────────────────────────
def test_a_holiday_has_no_session():
    assert session_for(date(2026, 7, 3)) is None       # observed Independence Day
    assert is_trading_day(date(2026, 7, 3)) is False


def test_a_half_day_closes_early_and_says_so():
    session = session_for(date(2026, 11, 27))
    assert session is not None and session.half_day
    assert session.minutes == 210                       # 09:30-13:00 ET


def test_dst_moves_the_open_by_an_hour():
    winter = session_for(date(2026, 1, 15))
    summer = session_for(date(2026, 7, 15))
    assert et_offset(date(2026, 1, 15)) == timedelta(hours=-5)
    assert et_offset(date(2026, 7, 15)) == timedelta(hours=-4)
    assert winter.open_utc.hour == 14 and summer.open_utc.hour == 13


def test_an_unknown_year_is_refused_not_guessed():
    with pytest.raises(CalendarError):
        session_for(date(2031, 3, 3))


def test_expiry_is_the_close_not_midnight():
    assert expires_at_close(date(2026, 9, 1)).hour == 20      # 16:00 ET in DST
    assert expires_at_close(date(2026, 11, 27)).hour == 18    # half day


def test_trading_days_skip_weekends_and_holidays():
    days = trading_days(date(2026, 9, 4), date(2026, 9, 9))
    assert date(2026, 9, 7) not in days          # Labor Day
    assert date(2026, 9, 5) not in days          # Saturday
    assert days == (date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9))


def test_the_calendar_report_counts_minutes():
    payload = report(date(2026, 11, 25), date(2026, 11, 30))
    assert payload["sessions"] == 3               # 25th, 27th (half), 30th
    assert payload["half_days"] == ["2026-11-27"]


# ── coverage ────────────────────────────────────────────────────────────────
def test_a_trade_inside_its_bars_is_measurable():
    row = classify(trade(), barset([1.0, 1.1, 1.2, 1.3, 1.4, 1.5]))
    assert row.measurable and row.reason == ""


def test_no_bars_and_a_broken_clock_are_different_failures():
    assert classify(trade(), None).reason == "no_bars"
    other = classify(trade(), barset([1.0, 1.1, 1.2], day=date(2026, 8, 28)))
    assert other.reason == "clock_skew"


def test_an_entry_before_the_first_bar_is_named():
    late = barset([1.0, 1.1, 1.2], start_hour=15)
    row = classify(trade(), late)
    assert row.reason == "entry_before_bars" and "first bar" in row.detail


def test_an_entry_after_the_last_bar_is_named():
    early = barset([1.0, 1.1, 1.2], start_hour=13, minute_step=1)
    row = classify(trade(at=datetime(2026, 9, 1, 19, 0, tzinfo=UTC)), early)
    assert row.reason == "entry_after_bars"


def test_a_trade_with_no_exit_is_refused_before_anything_else():
    row = classify(trade(exit_pct=None, exit_price=None), barset([1.0, 1.1]))
    assert row.reason == "no_exit"


def test_a_pre_market_entry_is_outside_the_session():
    row = classify(trade(at=datetime(2026, 9, 1, 11, 0, tzinfo=UTC)),
                   barset([1.0, 1.1, 1.2]))
    assert row.reason == "entry_outside_session"


def test_a_set_spanning_two_sessions_is_refused_by_name():
    """The shipped corpus has two such sets; a mark-out over them measures an overnight move."""
    first = barset([1.0, 1.1])
    second_ts = datetime(2026, 9, 2, 13, 30, tzinfo=UTC)
    second = Bar(ts_utc=second_ts, open=D("1.2"), high=D("1.2"), low=D("1.2"), close=D("1.2"))
    combined = BarSet(ticker="QQQ", side="PUT", strike=D("710"), day=DAY,
                      bars=first.bars + (second,))
    assert "2 sessions" in (spans_sessions(combined) or "")
    row = classify(trade(), combined)
    assert row.reason == "multi_session_bars"


def test_assess_summarises_by_reason_and_channel():
    rows = [trade(), trade(), trade(exit_pct=None, exit_price=None)]
    ledger = SimpleNamespace(trades=rows)
    classified, summary = assess(ledger, {"QQQ|PUT|710|2026-09-01": barset([1.0, 1.1, 1.2])})
    assert summary["trades"] == 3
    assert summary["measurable"] == 2
    assert summary["by_reason"] == {"no_exit": 1}
    assert summary["by_channel"]["etf-options"]["measurable"] == 2
    assert len(classified) == 3

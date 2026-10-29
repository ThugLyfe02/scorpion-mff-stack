"""Mark-out: entry drift, capture, and the refusals that keep both honest."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from scorpion.markout import Bar, BarSet, aggregate, analyse, key_for, load_bars

D = Decimal
T0 = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)


def barset(prices: list[float], *, ticker="QQQ", side="PUT", strike="710", day="2026-09-01",
           interval=5, wick=0.0) -> BarSet:
    """`wick` inflates high and deflates low: a path with no wick has MFE == max close."""
    bars = tuple(
        Bar(T0 + timedelta(minutes=interval * i), D(str(p)), D(str(p * (1 + wick))),
            D(str(p * (1 - wick / 2))), D(str(p)))
        for i, p in enumerate(prices)
    )
    return BarSet(contract=f"{ticker}_{side.lower()}_{strike}_{day}", ticker=ticker,
                  side=side, strike=D(strike), day=day, bars=bars)


def trade(entry_px="1.00", exit_px="1.50", at=T0, ticker="QQQ", side="PUT", strike="710",
          day=date(2026, 9, 1)):
    return SimpleNamespace(ticker=ticker, side=side, strike=D(strike), day=day,
                           entry_ts_utc=at, entry_bid=D(entry_px),
                           exit_price=D(exit_px) if exit_px else None)


def test_entry_drift_is_the_fifteen_minute_mark_against_the_entry():
    bs = barset([1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9])
    out = analyse(trade(entry_px="1.00"), bs)
    assert out.measurable
    assert out.hours[15] == D("1.3")
    assert out.entry_drift == D("0.3000")


def test_capture_is_realised_over_the_available_move():
    bs = barset([1.0, 1.0, 1.0, 1.0, 1.0, 1.0], wick=0.2)
    # mfe = 1.2 (high = close * 1.2) on a 1.00 entry; exit 1.50 -> realised 0.5, avail 0.2
    out = analyse(trade(entry_px="1.00", exit_px="1.50"), bs)
    assert out.mfe == D("1.2")
    assert out.available == D("0.2000")
    assert out.capture == D("2.5000")          # sold above the best mark in this synthetic


def test_a_pre_market_entry_is_refused_not_averaged():
    bs = barset([1.0, 1.1, 1.2])
    early = trade(at=T0 - timedelta(hours=3))
    out = analyse(early, bs)
    assert not out.measurable and out.reason == "entry_outside_coverage"


def test_a_missing_bar_set_is_named():
    out = analyse(trade(), None)
    assert not out.measurable and out.reason == "no_bars"


def test_aggregates_are_withheld_below_the_floor():
    bs = barset([1.0, 1.1, 1.2, 1.3, 1.4])
    rows = [analyse(trade(), bs) for _ in range(3)]
    payload = aggregate(rows, floor=5)
    assert "withheld" in payload
    assert payload["coverage"]["measurable"] == 3


def test_aggregates_report_exclusions_by_reason():
    bs = barset([1.0, 1.1, 1.2, 1.3, 1.4])
    rows = [analyse(trade(), bs) for _ in range(6)]
    rows.append(analyse(trade(at=T0 - timedelta(hours=2)), bs))
    payload = aggregate(rows, floor=5)
    assert payload["coverage"]["measurable"] == 6
    assert payload["coverage"]["excluded"] == {"entry_outside_coverage": 1}
    assert payload["median_capture"] is not None


def test_capture_floor_below_035_names_exit_discipline():
    # the premium touched 1.30 and he left at 1.05: a sixth of the move kept
    bs = barset([1.0, 1.3, 1.3, 1.3, 1.3])
    rows = [analyse(trade(entry_px="1.00", exit_px="1.05"), bs) for _ in range(6)]
    payload = aggregate(rows)
    assert payload["verdict"].startswith("exit_discipline")
    assert payload["median_capture"] == 0.1667      # aggregate reports floats, not Decimals


def test_a_working_system_is_not_accused_of_a_failure_mode():
    bs = barset([1.0, 1.1, 1.2, 1.3, 1.4, 1.5])
    rows = [analyse(trade(entry_px="1.00", exit_px="1.45"), bs) for _ in range(6)]
    assert aggregate(rows)["verdict"] == "entry_and_exit_both_working"


def test_loading_skips_sets_that_are_not_a_path(tmp_path):
    payload = {
        "QQQ_put_710_2026-09-01": {"bars": [
            {"begins_at": "2026-09-01T13:30:00Z", "open": "1", "high": "1.1",
             "low": "0.9", "close": "1.0"},
            {"begins_at": "2026-09-01T13:35:00Z", "open": "1", "high": "1.2",
             "low": "0.9", "close": "1.1"},
        ]},
        "SPY_call_600_2026-09-02": {"bars": [
            {"begins_at": "2026-09-02T13:30:00Z", "open": "1", "high": "1.1",
             "low": "0.9", "close": "1.0"},
        ]},
        "garbage": {"bars": []},
    }
    path = tmp_path / "bars.json"
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    loaded = load_bars(path)
    assert list(loaded) == ["QQQ|PUT|710|2026-09-01"]


def test_key_for_matches_the_loader_convention():
    assert key_for(trade()) == "QQQ|PUT|710|2026-09-01"

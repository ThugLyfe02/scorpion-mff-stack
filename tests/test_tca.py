"""Execution quality: bar joining, measurable-gating, carry cost, and fill costs.

The most important tests here are the *refusals*: the path aggregates must withhold
themselves when too few trades are measurable, unmeasurable rows must not enter a
median, and the fill report must say "no fills" rather than print a zero.
"""

from __future__ import annotations

import json

import pytest

from scorpion_mff.tca import (
    Bar,
    BarSet,
    Execution,
    ExecutionLog,
    FillReport,
    analyse_paths,
    analyse_trade,
    join_to_trades,
    load_barsets,
)

ET = 4 * 3600
DAY_TS = 1_757_000_000.0


def barset(*, date="2026-08-25", ticker="QQQ", side="call", strike=700.0,
           start=DAY_TS, closes=((1.00, 1.20, 0.90, 1.10), (1.10, 1.30, 1.00, 1.25)),
           step=300) -> BarSet:
    bars = []
    for i, (o, h, low, c) in enumerate(closes):
        bars.append(Bar(ts=start + i * step, open=o, high=h, low=low, close=c))
    return BarSet(date=date, ticker=ticker, side=side, strike=strike, bars=tuple(bars))


class Trade:
    """The slice of historic.Trade that tca touches."""

    def __init__(self, *, date="2026-08-25", time_et="10:00", channel="etf-options",
                 ticker="QQQ", side="call", strike="700", expiry="2026-08-25",
                 entry_bid=1.0, exit_pct=50.0, entry_ts=DAY_TS, exit_ts=DAY_TS + 600):
        self.date, self.time_et, self.channel = date, time_et, channel
        self.ticker, self.side, self.strike, self.expiry = ticker, side, strike, expiry
        self.entry_bid, self.exit_pct = entry_bid, exit_pct
        self._entry_ts, self._exit_ts = entry_ts, exit_ts

    @property
    def entry_ts(self):
        return self._entry_ts

    @property
    def exit_ts(self):
        return self._exit_ts

    @property
    def exit_price(self):
        return None if self.exit_pct is None else round(self.entry_bid * (1 + self.exit_pct / 100), 4)


# ── loading + joining ──────────────────────────────────────────────────────
def test_loader_reads_trade_series_and_skips_other_schemes(tmp_path):
    good = tmp_path / "trade_01_2026-08-25_QQQ_call_700.json"
    good.write_text(json.dumps({"trade_index": 1, "bars": [
        {"begins_at": "2026-08-25T14:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5}]}))
    other = tmp_path / "2026-08-25_abc-instrument.json"
    other.write_text(json.dumps({"instrument_id": "abc", "bars": [
        {"begins_at": "2026-08-25T14:00:00Z", "open": 1, "high": 1, "low": 1, "close": 1}]}))
    empty = tmp_path / "trade_02_2026-08-25_QQQ_put_690.json"
    empty.write_text(json.dumps({"trade_index": 2, "bars": []}))

    loaded = load_barsets(tmp_path)
    assert len(loaded) == 1 and loaded[0].ticker == "QQQ" and loaded[0].strike == 700.0


def test_join_is_by_content_not_by_filename_index():
    """The filename's trade_index is from the bar-fetch pass, not a trades.json row."""
    bs = barset(ticker="NVDA", side="put", strike=225.0)
    trade = Trade(ticker="NVDA", side="put", strike="225")
    joined, unmatched = join_to_trades([bs], [trade])
    assert len(joined) == 1 and not unmatched


def test_strike_format_does_not_break_the_join():
    bs = barset(strike=227.5)
    joined, _ = join_to_trades([bs], [Trade(strike="227.50")])
    assert len(joined) == 1


def test_unmatched_series_are_reported_not_dropped():
    report = analyse_paths([barset(ticker="ZZZZ")], [Trade()])
    assert report.n == 0 and len(report.unmatched) == 1


# ── the measurable gate ────────────────────────────────────────────────────
def test_entry_before_coverage_is_not_measurable():
    """The corpus's real pattern: a 06:47 ET entry against bars that start 09:30."""
    trade = Trade(entry_ts=DAY_TS - 4 * 3600, exit_ts=DAY_TS - 3 * 3600)
    path = analyse_trade(trade, barset(start=DAY_TS))
    assert path.measurable is False
    assert path.mfe_pct is None and path.mae_pct is None
    assert "not measurable" in path.note


def test_an_inside_trade_is_measurable_and_gets_path_metrics():
    trade = Trade(entry_ts=DAY_TS, exit_ts=DAY_TS + 300)
    path = analyse_trade(trade, barset(start=DAY_TS))
    assert path.measurable is True
    # window = bars at/inside the exit: highs 1.20 & 1.30, lows 0.90 & 1.00
    assert path.mfe_pct == pytest.approx((1.30 / 1.05 - 1) * 100, abs=0.01)
    assert path.mae_pct == pytest.approx((0.90 / 1.05 - 1) * 100, abs=0.01)


def test_aggregates_are_withheld_below_the_floor():
    report = analyse_paths([barset()], [Trade(entry_ts=DAY_TS, exit_ts=DAY_TS + 300)])
    assert len(report.measurable) == 1 and not report.measurable_enough
    text = report.to_text()
    assert "path aggregates withheld" in text
    assert "MAE" not in text.split("carry:")[0].replace("MAE  median", "")


def test_unmeasurable_rows_cannot_pollute_the_median():
    """The pre-market row must not move the path median: it has no path."""
    measurable_sets = [barset(strike=700.0 + i) for i in range(5)]
    measurable_trades = [Trade(strike=str(700 + i), entry_ts=DAY_TS, exit_ts=DAY_TS + 300)
                         for i in range(5)]
    late_set = barset(strike=800.0)
    late_trade = Trade(strike="800", entry_ts=DAY_TS - 4 * 3600, exit_ts=DAY_TS - 3 * 3600)

    clean = analyse_paths(measurable_sets, measurable_trades)
    dirty = analyse_paths(measurable_sets + [late_set], measurable_trades + [late_trade])
    assert len(clean.measurable) == 5 and len(dirty.measurable) == 5
    assert dirty.n == 6, "the pre-market trade must still be reported"
    assert dirty.mae_median == pytest.approx(clean.mae_median), \
        "a row with no measurable path must not move the median"
    unmeasurable = [r for r in dirty.rows if not r.measurable]
    assert len(unmeasurable) == 1 and unmeasurable[0].mae_pct is None


# ── carry cost + expiry (measurable for every joined trade) ────────────────
def test_carry_is_the_first_covered_close_against_entry():
    trade = Trade(entry_ts=DAY_TS - 4 * 3600)          # entered pre-market
    path = analyse_trade(trade, barset(start=DAY_TS))
    assert path.carry_pct == pytest.approx((1.10 / 1.05 - 1) * 100, abs=0.01)


def test_expiry_is_detected_from_a_zero_close():
    closes = ((1.0, 1.0, 1.0, 1.0), (0.5, 0.5, 0.5, 0.5), (0.01, 0.01, 0.01, 0.01))
    path = analyse_trade(Trade(entry_ts=DAY_TS), barset(start=DAY_TS, closes=closes))
    assert path.expired is True


def test_a_live_contract_is_not_marked_expired():
    path = analyse_trade(Trade(entry_ts=DAY_TS), barset(start=DAY_TS))
    assert path.expired is False


# ── venue fills ────────────────────────────────────────────────────────────
def execution(**kw) -> Execution:
    base = dict(contract_key="QQQ|CALL|700|0DTE", action="buy", qty=10, limit=1.00,
                fill_price=1.03, fill_qty=10, intent_ts=DAY_TS, fill_ts=DAY_TS + 0.4,
                channel="etf-options", reference=0.95)
    base.update(kw)
    return Execution(**base)


def test_a_buy_above_the_limit_costs_money():
    e = execution()
    assert e.cost_per_contract == pytest.approx(0.03)
    assert e.cost_usd == pytest.approx(30.0)
    assert e.cost_bps == pytest.approx(300.0)


def test_a_sell_below_the_limit_costs_money():
    e = execution(action="sell", limit=2.00, fill_price=1.95)
    assert e.cost_per_contract == pytest.approx(0.05)
    assert e.cost_usd == pytest.approx(50.0)


def test_a_better_fill_is_negative_cost():
    e = execution(fill_price=0.98)
    assert e.cost_per_contract < 0 and e.cost_usd < 0


def test_partial_fill_is_visible():
    e = execution(fill_qty=4)
    assert e.fill_ratio == pytest.approx(0.4)
    report = FillReport(executions=[e])
    assert report.partial_share == 100.0


def test_through_bid_measures_what_we_paid_over_his_post():
    assert execution(fill_price=1.0455).through_bid == pytest.approx(1.1005, abs=1e-4)


def test_suggested_multiple_needs_ten_fills():
    assert FillReport(executions=[execution() for _ in range(5)]).suggested_ask_multiple() is None
    fills = [execution(fill_price=1.0 + i * 0.01) for i in range(10)]
    suggested = FillReport(executions=fills).suggested_ask_multiple()
    assert suggested is not None and suggested >= 1.0


def test_suggested_multiple_rounds_up_never_down():
    fills = [execution(fill_price=1.051) for _ in range(10)]
    suggested = FillReport(executions=fills).suggested_ask_multiple()
    assert suggested >= 1.051 / 0.95 - 1e-9


def test_no_fills_says_so_instead_of_printing_zeros():
    text = FillReport().to_text()
    assert "no venue fills recorded yet" in text
    assert "0.0 bps" not in text


def test_execution_log_round_trips(tmp_path):
    path = tmp_path / "executions.jsonl"
    log = ExecutionLog(path)
    log.record(execution())
    again = ExecutionLog.load(path)
    assert len(again.records) == 1
    assert again.records[0].fill_price == pytest.approx(1.03)
    assert again.records[0].reference == pytest.approx(0.95)


def test_a_torn_execution_line_is_skipped(tmp_path):
    path = tmp_path / "executions.jsonl"
    path.write_text('{"contract_key": "x", "action": "buy"\n' + json.dumps(execution().to_dict()) + "\n")
    assert len(ExecutionLog.load(path).records) == 1


def test_missing_execution_log_is_empty_not_an_error(tmp_path):
    assert ExecutionLog.load(tmp_path / "absent.jsonl").records == []


# ── against the real repo data ─────────────────────────────────────────────
def test_the_real_bars_join_and_the_report_is_honest():
    from scorpion_mff import historic
    barsets = load_barsets()
    if not barsets:
        pytest.skip("no bar series in this checkout")
    report = analyse_paths(barsets, historic.load_trades())
    assert report.n > 0, "the real series must join to the corpus"
    # The finding this test protects: most entries are pre-market, so the path
    # aggregates must NOT be quoted as if they covered the corpus.
    assert len(report.measurable) < report.n
    if not report.measurable_enough:
        assert "withheld" in report.to_text()

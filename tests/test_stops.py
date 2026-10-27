"""The breakeven stop the engine armed but never enforced, and the marks view.

The lock says "trim to 1 + BE". The engine writes `stop->be@...` into the fill log
and sets `Position.breakeven_stop`, but nothing read it back. These tests pin the
gap and the fix: a bid at or through the stop flattens the remainder at the bid
actually seen, and the marks view withholds a total it cannot support.
"""

from __future__ import annotations

import pytest

from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.stops import (
    book_from_fills,
    breakeven_exit,
    breakeven_triggers,
    load_bids,
    mark_book,
)
from scorpion_mff.types import Fill, Position, Quote, Side


def pos(key="QQQ|CALL|719|0DTE", contracts=1, avg=1.0, be=None) -> Position:
    return Position(contract_key=key, ticker="QQQ", side=Side.CALL, contracts=contracts,
                    avg_price=avg, opened_ts=0.0, breakeven_stop=be)


# ── the stop itself ──────────────────────────────────────────────────────────
def test_no_stop_armed_means_no_exit():
    assert breakeven_exit(pos(be=None), 0.01) is None


def test_bid_above_the_stop_does_not_fire():
    assert breakeven_exit(pos(be=1.0), 1.05) is None


def test_bid_at_the_stop_flattens_the_whole_remainder():
    intent = breakeven_exit(pos(contracts=1, avg=1.0, be=1.0), 1.0)
    assert intent is not None
    assert intent.action == "sell"
    assert intent.qty == 1
    assert intent.limit == 1.0          # filled at the bid seen, not the stop price


def test_a_gap_through_the_stop_is_priced_at_the_bid_not_the_stop():
    intent = breakeven_exit(pos(contracts=1, avg=1.0, be=1.0), 0.85)
    assert intent is not None
    assert intent.limit == 0.85         # honest realised P&L, not a fiction


def test_triggers_only_fires_armed_positions_with_a_bid():
    book = [pos("QQQ|CALL|719|0DTE", be=1.0), pos("SPY|PUT|500|0DTE", be=1.0),
            pos("IWM|CALL|200|0DTE", be=None)]
    intents = breakeven_triggers(book, {"QQQ|CALL|719|0DTE": 0.90,
                                        "SPY|PUT|500|0DTE": 1.10})
    assert [i.contract_key for i in intents] == ["QQQ|CALL|719|0DTE"]


# ── the marks view ───────────────────────────────────────────────────────────
def test_mark_book_prices_open_positions_and_withholds_a_partial_total():
    book = [pos("QQQ|CALL|719|0DTE", contracts=2, avg=1.0),
            pos("SPY|PUT|500|0DTE", contracts=1, avg=3.0)]
    marks = mark_book(book, {"QQQ|CALL|719|0DTE": 1.25})
    assert marks.priced == 1
    assert marks.unpriced == 1
    assert marks.total_unrealized_usd is None      # a partial total would lie
    assert marks.marks[0].unrealized_usd == 50.0   # (1.25 - 1.0) * 2 * 100


def test_mark_book_sums_when_everything_is_priced():
    book = [pos("QQQ|CALL|719|0DTE", contracts=2, avg=1.0, be=1.0),
            pos("SPY|PUT|500|0DTE", contracts=1, avg=3.0)]
    marks = mark_book(book, {"QQQ|CALL|719|0DTE": 1.25, "SPY|PUT|500|0DTE": 2.50})
    assert marks.total_unrealized_usd == 0.0       # +50 - 50
    assert marks.stopped_count == 0                # QQQ bid 1.25 > be 1.0


# ── reconstruction from the fill log ─────────────────────────────────────────
def _fill(seq, event, key, qty, price, accepted=True, note="") -> Fill:
    return Fill(seq=seq, ts=float(seq), event=event, contract_key=key, qty=qty,
                price=price, reason="test", accepted=accepted, note=note)


def test_book_from_fills_rebuilds_weighted_cost_and_arms_the_trim_stop():
    fills = [
        _fill(1, "buy", "QQQ|CALL|719|0DTE", 10, 1.00, note="etf-options"),
        _fill(2, "buy", "QQQ|CALL|719|0DTE", 10, 2.00, note="add"),
        _fill(3, "sell", "QQQ|CALL|719|0DTE", 19, 2.20, note="stop->be@1.5"),
    ]
    book = book_from_fills(fills)
    assert list(book) == ["QQQ|CALL|719|0DTE"]
    position = book["QQQ|CALL|719|0DTE"]
    assert position.contracts == 1               # 20 bought, 19 sold -> 1 runner
    assert position.avg_price == 1.5             # (10*1 + 10*2) / 20
    assert position.breakeven_stop == 1.5        # armed at trim time


def test_book_from_fills_does_not_arm_a_stop_on_a_full_close():
    fills = [
        _fill(1, "buy", "QQQ|CALL|719|0DTE", 5, 1.00),
        _fill(2, "sell", "QQQ|CALL|719|0DTE", 5, 1.20, note="flat"),
    ]
    assert book_from_fills(fills) == {}


def test_load_bids_accepts_the_tape_shape_and_a_plain_map():
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "quotes.json"
        p.write_text(json.dumps({"quotes": {"QQQ|CALL|719|0DTE": [1.00, 1.05]}}),
                     encoding="utf-8")
        assert load_bids(p) == {"QQQ|CALL|719|0DTE": 1.00}

        p.write_text(json.dumps({"QQQ|CALL|719|0DTE": {"bid": 0.95, "ask": 1.00},
                                 "junk": "nonsense"}), encoding="utf-8")
        assert load_bids(p) == {"QQQ|CALL|719|0DTE": 0.95}


# ── engine integration: the dead field, made live ───────────────────────────
def test_engine_on_quote_is_off_unless_opted_in():
    eng = Engine(config=EngineConfig(enforce_breakeven=False), fills=FillLog(),
                 clock=lambda: 1000.0)
    key = "QQQ|CALL|719|0DTE"
    eng.positions[key] = pos(key, contracts=1, avg=1.0, be=1.0)
    assert eng.on_quote(key, Quote(bid=0.80, ask=0.85)) == []
    assert key in eng.positions                      # nothing happened


def test_engine_on_quote_flattens_at_the_stop_and_realises_the_loss_honestly():
    eng = Engine(config=EngineConfig(enforce_breakeven=True), fills=FillLog(),
                 clock=lambda: 1000.0)
    key = "QQQ|CALL|719|0DTE"
    eng.positions[key] = pos(key, contracts=2, avg=1.0, be=1.0)
    intents = eng.on_quote(key, Quote(bid=0.90, ask=0.95))
    assert [i.action for i in intents] == ["sell"]
    assert intents[0].qty == 2
    assert intents[0].limit == 0.90
    assert key not in eng.positions
    assert key in eng.closed
    assert eng.stats.realized_usd == pytest.approx(-20.0)   # (0.90-1.0)*2*100
    sells = [f for f in eng.fills.records if f.event == "sell"]
    assert sells and sells[-1].note == "breakeven_stop"


def test_engine_on_quote_leaves_a_position_above_its_stop_alone():
    eng = Engine(config=EngineConfig(enforce_breakeven=True), fills=FillLog(),
                 clock=lambda: 1000.0)
    key = "QQQ|CALL|719|0DTE"
    eng.positions[key] = pos(key, contracts=1, avg=1.0, be=1.0)
    assert eng.on_quote(key, Quote(bid=1.01, ask=1.05)) == []
    assert key in eng.positions


# ── the `marks --check` cron tripwire ───────────────────────────────────────
def _marks_args(tmp_path, *, check=True):
    import argparse
    return argparse.Namespace(
        fills=tmp_path / "fills.jsonl",
        quotes=tmp_path / "quotes.json",
        json=False,
        check=check,
    )


def _write_book_and_quotes(tmp_path, bid):
    import json

    log = FillLog(path=tmp_path / "fills.jsonl")
    log.record(event="buy", contract_key="QQQ|CALL|719|0DTE", qty=2, price=1.0,
               reason="entry", note="etf-options")
    log.record(event="sell", contract_key="QQQ|CALL|719|0DTE", qty=1, price=1.0,
               reason="trim", note="stop->be@1.0")
    (tmp_path / "quotes.json").write_text(
        json.dumps({"QQQ|CALL|719|0DTE": {"bid": bid, "ask": bid + 0.05}}),
        encoding="utf-8",
    )


def test_marks_check_exits_one_when_an_armed_stop_is_through(tmp_path, capsys):
    from scorpion_mff.cli import cmd_marks

    _write_book_and_quotes(tmp_path, bid=0.90)          # through the 1.00 stop
    assert cmd_marks(_marks_args(tmp_path)) == 1
    assert "STOP" in capsys.readouterr().out


def test_marks_check_exits_zero_when_the_book_is_above_its_stops(tmp_path):
    from scorpion_mff.cli import cmd_marks

    _write_book_and_quotes(tmp_path, bid=1.10)          # above the 1.00 stop
    assert cmd_marks(_marks_args(tmp_path)) == 0


def test_marks_check_exits_one_when_the_book_cannot_be_priced(tmp_path):
    from scorpion_mff.cli import cmd_marks

    log = FillLog(path=tmp_path / "fills.jsonl")
    log.record(event="buy", contract_key="QQQ|CALL|719|0DTE", qty=2, price=1.0,
               reason="entry")
    (tmp_path / "quotes.json").write_text("{}", encoding="utf-8")
    assert cmd_marks(_marks_args(tmp_path)) == 1         # withheld, not zero

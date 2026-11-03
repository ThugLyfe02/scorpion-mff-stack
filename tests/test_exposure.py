"""The correlated-book cap: pure arithmetic, the engine wiring, and the fill-log rebuild."""

from __future__ import annotations

import json

import pytest

from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.exposure import (
    ExposureLimits,
    book_exposure,
    expiry_of,
    guard,
    load_limits,
    positions_from_fills,
    premium_usd,
)
from scorpion_mff.fills import FillLog
from scorpion_mff.parser import parse
from scorpion_mff.types import AlertKind, Fill, Position, Quote, RawMessage, Side, Signal

KING = "options-king"
KING_ID = "968352649437126676"
DAY = 1_757_000_000.0


def pos(ticker: str = "QQQ", side: Side = Side.CALL, contracts: int = 1,
        avg: float = 1.0, strike: float = 600.0, expiry: str = "0DTE") -> Position:
    return Position(
        contract_key=f"{ticker}|{side.value}|{strike:g}|{expiry}",
        ticker=ticker, side=side, contracts=contracts, avg_price=avg, opened_ts=0.0,
    )


def sig(ticker: str = "QQQ", side: Side = Side.CALL,
        expiry: str = "0DTE", strike: float = 600.0) -> Signal:
    return Signal(kind=AlertKind.ENTRY, ticker=ticker, side=side, strike=strike,
                  expiry=expiry)


class FixedQuotes:
    def __init__(self, bid: float = 1.0, ask: float = 1.0):
        self.bid, self.ask = bid, ask

    def quote(self, key: str) -> Quote:
        return Quote(self.bid, self.ask)


def raw_entry(ticker: str = "QQQ", strike: str = "600", ts: float = DAY) -> RawMessage:
    return RawMessage(
        channel=KING, channel_id=KING_ID,
        text=f"CALL TICKER {ticker} STRIKE ${strike} Exp 0DTE Current BID Price $1.00",
        ts=ts,
    )


def engine(**kwargs) -> Engine:
    return Engine(config=EngineConfig(pipe_test=False), fills=FillLog(),
                  quotes=FixedQuotes(), **kwargs)


# ── helpers ───────────────────────────────────────────────────────────────────


def test_premium_and_expiry_helpers():
    assert premium_usd(pos(contracts=3, avg=2.5)) == pytest.approx(750.0)
    assert expiry_of("QQQ|CALL|600|0DTE") == "0DTE"
    assert expiry_of("no-separator") == "no-separator"


# ── the guard, pure ───────────────────────────────────────────────────────────


def test_guard_allows_an_empty_book():
    decision = guard(ExposureLimits(), [], sig(), incoming_qty=1, incoming_price=1.0)
    assert decision.allowed is True
    assert decision.reason == "within_limits"


def test_guard_refuses_a_second_position_in_the_same_underlying():
    decision = guard(ExposureLimits(), [pos("QQQ")], sig("QQQ"))
    assert decision.allowed is False
    assert "underlying" in decision.reason


def test_guard_allows_a_different_underlying():
    decision = guard(ExposureLimits(), [pos("QQQ")], sig("SPY"))
    assert decision.allowed is True


def test_guard_refuses_an_expiry_cluster_above_the_cap():
    limits = ExposureLimits(max_per_expiry=2)
    crowded = [pos("QQQ", expiry="0DTE"), pos("SPY", expiry="0DTE")]
    assert guard(limits, crowded, sig("NVDA", expiry="0DTE")).allowed is False
    # one open position sharing the expiry is still inside a cap of two
    assert guard(limits, [pos("QQQ", expiry="0DTE")], sig("NVDA", expiry="0DTE")).allowed is True


def test_guard_refuses_a_premium_cap_breach():
    limits = ExposureLimits(max_per_underlying=2, max_premium_per_underlying_usd=200)
    # held 100 + incoming 200 = 300 > 200 (the count check passes: cap allows 2 in QQQ)
    decision = guard(limits, [pos("QQQ", contracts=1, avg=1.0)], sig("QQQ"),
                     incoming_qty=2, incoming_price=1.0)
    assert decision.allowed is False
    assert "premium" in decision.reason


def test_guard_allows_premium_within_the_cap():
    limits = ExposureLimits(max_per_underlying=2, max_premium_per_underlying_usd=500)
    assert guard(limits, [pos("QQQ", contracts=1, avg=1.0)], sig("QQQ"),
                 incoming_qty=2, incoming_price=1.0).allowed is True


def test_guard_ignores_closed_positions():
    closed = pos("QQQ", contracts=0)
    decision = guard(ExposureLimits(), [closed], sig("QQQ"))
    assert decision.allowed is True


# ── the book ──────────────────────────────────────────────────────────────────


def test_book_exposure_net_is_calls_minus_puts():
    book = [pos("QQQ", Side.CALL, contracts=2, avg=1.5),
            pos("SPY", Side.PUT, contracts=1, avg=2.0)]
    exposure = book_exposure(book)
    assert exposure["positions"] == 2
    assert exposure["gross_premium_usd"] == pytest.approx(300.0 + 200.0)
    assert exposure["net_premium_usd"] == pytest.approx(300.0 - 200.0)
    assert exposure["per_underlying"]["qqq"]["calls"] == 2
    assert exposure["per_underlying"]["spy"]["puts"] == 1
    assert exposure["per_expiry"]["0DTE"]["positions"] == 2


# ── limits file ───────────────────────────────────────────────────────────────


def test_load_limits_reads_and_defaults(tmp_path):
    path = tmp_path / "exposure.json"
    path.write_text(json.dumps({"max_per_underlying": 2,
                                "max_premium_per_underlying_usd": 500}), encoding="utf-8")
    limits = load_limits(path)
    assert limits.max_per_underlying == 2
    assert limits.max_per_expiry == 2  # default
    assert limits.max_premium_per_underlying_usd == 500


def test_load_limits_refuses_bad_values(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"max_per_underlying": 0}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_limits(path)


# ── fill-log rebuild ──────────────────────────────────────────────────────────


def test_positions_from_fills_rebuilds_the_open_book():
    fills = [
        Fill(seq=1, ts=DAY, event="buy", contract_key="QQQ|CALL|600|0DTE",
             qty=15, price=1.0, reason="entry"),
        Fill(seq=2, ts=DAY + 60, event="buy", contract_key="SPY|PUT|500|0DTE",
             qty=10, price=2.0, reason="entry"),
        Fill(seq=3, ts=DAY + 120, event="sell", contract_key="QQQ|CALL|600|0DTE",
             qty=5, price=1.2, reason="trim"),
    ]
    positions = positions_from_fills(fills)
    assert len(positions) == 2
    qqq = next(p for p in positions if p.ticker == "QQQ")
    assert qqq.contracts == 10  # 15 bought, 5 trimmed
    assert qqq.avg_price == pytest.approx(1.0)
    spy = next(p for p in positions if p.ticker == "SPY")
    assert spy.contracts == 10
    assert spy.avg_price == pytest.approx(2.0)


# ── engine wiring ─────────────────────────────────────────────────────────────


def test_the_engine_refuses_a_second_same_underlying_entry():
    eng = engine(exposure_limits=ExposureLimits(max_per_underlying=1))
    first = eng.on_signal(parse(raw_entry()), now=DAY)
    assert first, "the first QQQ entry must trade"
    # Same ticker, different strike: a different contract, the same underlying bet.
    second = eng.on_signal(parse(raw_entry(strike="601", ts=DAY + 60)), now=DAY + 60)
    assert second == []
    assert eng.fills.records[-1].reason.startswith("exposure:underlying:QQQ")


def test_without_limits_two_same_underlying_entries_still_trade():
    eng = engine()  # exposure_limits defaults to None -> off
    assert eng.on_signal(parse(raw_entry()), now=DAY)
    assert eng.on_signal(parse(raw_entry(strike="601", ts=DAY + 60)), now=DAY + 60)

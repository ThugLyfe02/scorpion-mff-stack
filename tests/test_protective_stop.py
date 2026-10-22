"""The breakeven stop after a trim must actually sell the runner."""

from __future__ import annotations

import time

from scorpion_mff.broker import QuoteScript, StaticQuotes
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.execution import ExecutionBook
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.types import AlertKind, Quote, Side, Signal

KEY = "QQQ|CALL|719|0DTE"
ET = "etf-options"


def ts(text: str) -> float:
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M"))


def entry(at: float) -> Signal:
    return Signal(kind=AlertKind.ENTRY, channel=ET, ticker="QQQ", side=Side.CALL,
                  strike=719.0, expiry="0DTE", his_bid=1.0, raw="entry")


def trim(at: float) -> Signal:
    return Signal(kind=AlertKind.TRIM, channel=ET, ticker="QQQ", side=Side.CALL,
                  strike=719.0, expiry="0DTE", price=1.2, raw="runners at 1.20")


def engine_with_runner() -> Engine:
    eng = Engine(config=EngineConfig(pipe_test=False), fills=FillLog(),
                 clock=lambda: 1000.0)
    at = 1000.0
    eng.on_signal(entry(at), Quote(1.0, 1.02), now=at)
    eng.on_signal(trim(at + 60), Quote(1.19, 1.21), now=at + 60)
    assert eng.positions[KEY].contracts == 1
    assert eng.positions[KEY].breakeven_stop == eng.positions[KEY].avg_price
    return eng


def test_a_runner_above_breakeven_is_left_alone():
    eng = engine_with_runner()
    eng.quotes = StaticQuotes({KEY: (1.03, 1.04)})
    assert eng.protective_exits(now=1000.0) == []


def test_a_runner_at_or_below_breakeven_is_sold():
    eng = engine_with_runner()
    eng.quotes = StaticQuotes({KEY: (1.00, 1.01)})
    intents = eng.protective_exits(now=1000.0)
    assert len(intents) == 1
    assert intents[0].action == "sell"
    assert intents[0].qty == 1
    assert intents[0].limit == 1.00
    assert intents[0].reason.startswith("stop:be@")


def test_no_quote_never_guesses_a_stop_fill():
    eng = engine_with_runner()
    eng.quotes = StaticQuotes({})
    assert eng.protective_exits(now=1000.0) == []


def test_an_untrimmed_position_has_no_protective_stop():
    eng = Engine(config=EngineConfig(pipe_test=False), fills=FillLog(),
                 clock=lambda: 1000.0)
    eng.on_signal(entry(1000.0), Quote(1.0, 1.02), now=1000.0)
    eng.quotes = StaticQuotes({KEY: (0.10, 0.11)})
    assert eng.protective_exits(now=1000.0) == []


def test_the_listener_places_the_stop_and_closes_the_runner(tmp_path):
    from scorpion_mff.broker import PaperBroker
    from scorpion_mff.types import RawMessage

    book = ExecutionBook(path=tmp_path / "execution.jsonl")
    listener = Listener(
        transport=TapeTransport([]),
        engine=Engine(config=EngineConfig(pipe_test=False), fills=FillLog(),
                      clock=lambda: 1000.0),
        execution=book,
        broker=PaperBroker(),
        quotes=QuoteScript({KEY: [(1.0, 1.02), (1.19, 1.21)]}),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=lambda: 1000.0),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: 1000.0),
        clock=lambda: 1000.0)

    listener.handle(RawMessage(
        channel=ET, channel_id="1", ts=1000.0,
        text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.00"))
    listener.handle(RawMessage(
        channel=ET, channel_id="1", ts=1060.0,
        text="Runners at 1.20"))
    assert listener.engine.positions[KEY].contracts == 1
    assert book.open_positions()[0].contracts == 1

    quote = StaticQuotes({KEY: (1.00, 1.01)})
    listener.quotes = quote
    listener.engine.quotes = quote
    intents = listener.enforce_stops(now=1100.0)
    assert len(intents) == 1
    assert listener.engine.positions == {}
    assert book.fills[-1].reason.startswith("stop:be@")

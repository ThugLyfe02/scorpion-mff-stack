"""Regression tests for the defects an independent review (fusion lean stack:
deepseek-v4-pro architect + deepseek-v4-flash main) found in the first version.

Each test names the real post shape that would have tripped it.
"""

from __future__ import annotations

import pytest

from scorpion_mff.broker import QuoteScript
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.parser import parse
from scorpion_mff.types import AlertKind, Quote, RawMessage

KING = "options-king"
KING_ID = "968352649437126676"
ET = "etf-options"
ET_ID = "1231301953972207667"
DAY1 = 1_757_000_000.0          # 2025-09-03-ish
DAY2 = DAY1 + 86_400.0


def raw(text: str, channel: str = KING, channel_id: str = KING_ID, ts: float = DAY1) -> RawMessage:
    return RawMessage(channel=channel, channel_id=channel_id, text=text, ts=ts)


# ── "sold runners" is a TRIM, not a flatten ──────────────────────────────────
def test_sold_runners_trims_it_does_not_flatten():
    """The lock lists 'sold runners' under trim-to-1+BE. The first classifier
    let the word SOLD win and flattened the whole position."""
    s = parse(raw("Sold runners at 3.90"))
    assert s.kind is AlertKind.TRIM, "sold runners must trim, not flatten"


def test_closed_fully_still_flattens():
    s = parse(raw("Runners at $3.00 closed fully"))
    assert s.kind is AlertKind.CLOSE


def test_flatten_still_flattens():
    assert parse(raw("Flatten it here")).kind is AlertKind.CLOSE


# ── scaling in vs scaling out ────────────────────────────────────────────────
@pytest.mark.parametrize("text,kind", [
    ("Scaling in here at 1.40", AlertKind.ADD),
    ("Scale in 1.25", AlertKind.ADD),
    ("Scale out half at 2.10", AlertKind.TRIM),
    ("Scaling out into strength", AlertKind.TRIM),
])
def test_scale_direction_is_not_a_coin_flip(text, kind):
    assert parse(raw(text)).kind is kind


# ── negated verbs ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "don't add here",
    "Do not close yet",
    "not closing this one",
    "No adds above 1.50",
])
def test_a_negated_verb_is_not_an_instruction(text):
    s = parse(raw(text))
    assert s.kind is AlertKind.IGNORE
    assert s.actionable is False


def test_stand_down_still_beats_the_negation_rule():
    assert parse(raw("DO NOT BUY - late")).kind is AlertKind.STOP


# ── the daily roll ───────────────────────────────────────────────────────────
def _engine() -> Engine:
    return Engine(config=EngineConfig(), fills=FillLog(), clock=lambda: DAY1)


def test_never_chase_does_not_poison_the_next_day():
    eng = _engine()
    entry = raw("CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01", ET, ET_ID)
    eng.on_signal(parse(entry), Quote(1.01, 1.05), now=DAY1)
    eng.on_signal(parse(raw("Close at 1.11", ET, ET_ID)), Quote(1.11, 1.14), now=DAY1 + 60)
    # same day: blocked, never chased
    assert eng.on_signal(parse(entry), Quote(1.01, 1.05), now=DAY1 + 120) == []
    # next day: the very same contract key is tradeable again, and the first
    # trade of the new day is a fresh pipe test
    intents = eng.on_signal(parse(entry), Quote(1.01, 1.05), now=DAY2)
    assert len(intents) == 1
    assert intents[0].qty == 1
    assert any(f.event == "day_roll" for f in eng.fills.records)


def test_a_new_day_resets_the_pipe_test_counter():
    eng = _engine()
    # two day-1 trades, in two channels so each exit resolves unambiguously
    eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $700 Exp Today Current BID Price $1.00", ET, ET_ID)),
                  Quote(1.0, 1.0), now=DAY1)
    eng.on_signal(parse(raw("CALL TICKER NVDA STRIKE $180 Exp Today Current BID Price $1.00")),
                  Quote(1.0, 1.0), now=DAY1)
    assert eng.stats.trades_today == 2
    # flatten both before the day rolls, so the book is genuinely empty
    eng.on_signal(parse(raw("Close at 1.10", ET, ET_ID)), Quote(1.1, 1.12), now=DAY1 + 60)
    eng.on_signal(parse(raw("Close at 1.10")), Quote(1.1, 1.12), now=DAY1 + 120)
    assert not eng.open_positions

    intents = eng.on_signal(parse(raw("CALL TICKER IWM STRIKE $200 Exp Today Current BID Price $1.00", ET, ET_ID)),
                            Quote(1.0, 1.0), now=DAY2)
    assert eng.stats.trades_today == 1, "the new day's first trade is counted from zero"
    assert intents[0].qty == 1, "and it is a fresh 1-contract pipe test"


# ── ambiguous exits page a human ─────────────────────────────────────────────
def test_ambiguous_exit_raises_an_alert_rather_than_silence():
    eng = _engine()
    eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $700 Exp Today Current BID Price $1.00", ET, ET_ID)),
                  Quote(1.0, 1.0), now=DAY1)
    eng.on_signal(parse(raw("CALL TICKER SPY STRIKE $500 Exp Today Current BID Price $1.00", ET, ET_ID)),
                  Quote(1.0, 1.0), now=DAY1)
    assert eng.on_signal(parse(raw("Close at 1.25", ET, ET_ID)), Quote(1.25, 1.3), now=DAY1) == []
    alerts = [f for f in eng.fills.records if f.event == "alert"]
    assert alerts and "ambiguous_exit" in alerts[0].reason


def test_an_exit_with_one_position_is_not_an_alert():
    eng = _engine()
    eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $700 Exp Today Current BID Price $1.00", ET, ET_ID)),
                  Quote(1.0, 1.0), now=DAY1)
    sells = eng.on_signal(parse(raw("Close at 1.25", ET, ET_ID)), Quote(1.25, 1.3), now=DAY1)
    assert len(sells) == 1
    assert not [f for f in eng.fills.records if f.event == "alert"]


# ── duplicate delivery ───────────────────────────────────────────────────────
def test_a_redelivered_message_does_not_trade_twice(tmp_path):
    message = RawMessage(channel=ET, channel_id=ET_ID, ts=DAY1, message_id="1234567890",
                         text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01")
    listener = Listener(
        transport=TapeTransport([message]),
        engine=Engine(config=EngineConfig(), fills=FillLog(), clock=lambda: DAY1),
        broker=None,
        quotes=QuoteScript({"QQQ|CALL|719|0DTE": [(1.01, 1.05)]}),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=lambda: DAY1),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: DAY1),
        clock=lambda: DAY1,
    )
    from scorpion_mff.broker import PaperBroker
    listener.broker = PaperBroker()

    assert len(listener.handle(message)) == 1          # the real post trades
    assert listener.handle(message) == []              # the redelivery does not
    assert len(listener.engine.fills.bought()) == 1
    assert [f.reason for f in listener.engine.fills.records if f.event == "duplicate"] == \
        ["redelivered_message_id"]


def test_the_seen_set_is_bounded(tmp_path):
    listener = Listener(
        transport=TapeTransport([]),
        engine=Engine(config=EngineConfig(), fills=FillLog(), clock=lambda: DAY1),
        broker=None,
        quotes=QuoteScript({}),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=lambda: DAY1),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: DAY1),
        clock=lambda: DAY1,
        seen_max=3,
    )
    from scorpion_mff.broker import PaperBroker
    listener.broker = PaperBroker()
    for i in range(10):
        listener.handle(RawMessage(channel=KING, channel_id=KING_ID, ts=DAY1,
                                   message_id=f"m{i}", text="Printing"))
    assert len(listener.seen) == 3
    assert "m9" in listener.seen and "m0" not in listener.seen

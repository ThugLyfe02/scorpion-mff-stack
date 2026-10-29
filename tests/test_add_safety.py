"""An add is a new buy: it must pass the same stops the entry passed.

The add path was a seam in the hot path. It checked the kill switch but not the
engine's stand-down, and it never consulted the governor at all. Two failures
followed directly:

  * `STAND DOWN` then "adding at 1.30" increased exposure on a halted desk;
  * a daily stop, loss ladder or channel tilt could be undone by the next add.

Exits stay legal in both cases - the point is to stop *adding* risk, not to trap
a position.
"""

from __future__ import annotations

import time

from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.governor import GovernorState, make_governor
from scorpion_mff.policy import baseline
from scorpion_mff.types import AlertKind, Quote, Side, Signal

KING = "options-king"
DAY = 1_757_000_000.0
DAY_NAME = time.strftime("%Y-%m-%d", time.gmtime(DAY))


def entry(ts: float = DAY) -> Signal:
    return Signal(kind=AlertKind.ENTRY, channel=KING, ticker="QQQ", side=Side.CALL,
                  strike=600.0, expiry="0DTE", his_bid=1.0, raw="entry")


def add(ts: float = DAY) -> Signal:
    return Signal(kind=AlertKind.ADD, channel=KING, ticker="QQQ", side=Side.CALL,
                  strike=600.0, expiry="0DTE", his_bid=1.0, price=1.0, raw="adding")


def stop() -> Signal:
    return Signal(kind=AlertKind.STOP, channel=KING, raw="STAND DOWN")


def engine(*, policy=None, state: GovernorState | None = None,
           pipe_test: bool = False) -> Engine:
    p = policy or baseline(name="test")
    return Engine(
        config=EngineConfig(pipe_test=pipe_test),
        fills=FillLog(),
        governor=make_governor(p),
        risk_state=state or GovernorState(),
    )


def test_a_stand_down_halts_the_add_as_well_as_the_entry():
    eng = engine()
    opened = eng.on_signal(entry(), Quote(1.0, 1.0), now=DAY)
    assert opened and opened[0].qty == 15

    eng.on_signal(stop(), now=DAY + 30)
    position = eng.positions["QQQ|CALL|600|0DTE"]
    assert position.contracts == 15 and position.adds == 0

    added = eng.on_signal(add(), Quote(1.0, 1.0), now=DAY + 60)
    assert added == []
    assert eng.positions["QQQ|CALL|600|0DTE"].contracts == 15
    assert eng.positions["QQQ|CALL|600|0DTE"].adds == 0
    last = eng.fills.records[-1]
    assert last.event == "reject" and last.reason == "halted:stand-down"


def test_a_stand_down_still_permits_an_exit():
    eng = engine()
    eng.on_signal(entry(), Quote(1.0, 1.0), now=DAY)
    eng.on_signal(stop(), now=DAY + 30)

    closed = eng.on_signal(
        Signal(kind=AlertKind.CLOSE, channel=KING, ticker="QQQ", side=Side.CALL,
               strike=600.0, expiry="0DTE", price=1.1, raw="all out"),
        Quote(1.1, 1.1), now=DAY + 60)
    assert closed and closed[0].action == "sell"
    assert "QQQ|CALL|600|0DTE" not in eng.positions


def test_a_halving_governor_halves_the_add_clip():
    p = baseline(name="half", channel_multipliers={KING: 0.5})
    eng = engine(policy=p)
    assert eng.on_signal(entry(), Quote(1.0, 1.0), now=DAY)[0].qty == 7  # 15 * 0.5

    added = eng.on_signal(add(), Quote(1.0, 1.0), now=DAY + 60)
    assert added, "an add under a half-size policy still trades, at half size"
    # add clip $2250 at $1.00 = 22 contracts; the governor floors it to 11.
    assert added[0].qty == 11
    assert "governor=" in added[0].reason


def test_a_daily_stop_blocks_the_add_even_after_a_clean_entry():
    eng = engine()
    eng.on_signal(entry(), Quote(1.0, 1.0), now=DAY)
    # The loss lands after the entry; the next add must not be allowed to undo it.
    eng.governor_state.record_result(-4_000.0, DAY_NAME)

    added = eng.on_signal(add(), Quote(1.0, 1.0), now=DAY + 60)
    assert added == []
    last = eng.fills.records[-1]
    assert last.event == "skip" and last.reason.startswith("governor:daily_stop")


def test_the_kill_switch_blocks_an_add_but_not_an_exit():
    eng = engine()
    eng.on_signal(entry(), Quote(1.0, 1.0), now=DAY)
    eng.blocked = lambda: True

    assert eng.on_signal(add(), Quote(1.0, 1.0), now=DAY + 60) == []
    assert eng.fills.records[-1].reason == "halted:kill-switch"
    closed = eng.on_signal(
        Signal(kind=AlertKind.CLOSE, channel=KING, ticker="QQQ", side=Side.CALL,
               strike=600.0, expiry="0DTE", price=1.1, raw="all out"),
        Quote(1.1, 1.1), now=DAY + 90)
    assert closed and closed[0].action == "sell"

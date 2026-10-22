"""Lifecycle regressions: the bugs that make a working hot path go silent.

Each test here is a defect found by reading the code against its own stated rules,
reproduced before it was fixed:

1. An expired contract left its key in the book. The next day's `QQQ|CALL|719|0DTE`
   is the same string, so every later entry in that contract was skipped as
   `already_open` - silence, the exact failure the executor exists to remove.
2. A STOP halted entries but not adds, so "adding at 1.30" could increase exposure
   on a desk that had been told to stand down.
3. Adds bypassed the governor entirely, so a loss ladder or stand-down could be
   undone by the next add post.
4. An add post that did not restate the channel sized at the *standard* clip even
   when the position belonged to the high-confidence channel.
"""

from __future__ import annotations

import time

from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.governor import Decision
from scorpion_mff.types import AlertKind, Quote, Side, Signal

ET = "etf-options"
HIGH = "high-confidence-options"


def _ts(text: str) -> float:
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M"))


def _entry(ts: float, *, channel: str = ET, ticker: str = "QQQ",
           strike: float = 719.0, expiry: str = "0DTE",
           bid: float = 1.05) -> Signal:
    return Signal(kind=AlertKind.ENTRY, channel=channel, ticker=ticker, side=Side.CALL,
                  strike=strike, expiry=expiry, his_bid=bid, raw="entry")


def _add(ts: float, *, channel: str = ET, ticker: str = "QQQ",
         strike: float = 719.0, expiry: str = "0DTE", price: float = 1.10) -> Signal:
    return Signal(kind=AlertKind.ADD, channel=channel, ticker=ticker, side=Side.CALL,
                  strike=strike, expiry=expiry, his_bid=price, price=price, raw="adding")


def _engine(**cfg) -> Engine:
    return Engine(config=EngineConfig(pipe_test=False, **cfg), fills=FillLog())


def _reasons(eng: Engine) -> list[str]:
    return [f.reason for f in eng.fills.records if not f.accepted]


def test_expired_contract_key_can_be_entered_again_on_a_later_day():
    """A recurring 0DTE key must not become a permanent "already_open" tombstone."""
    eng = _engine()
    day1 = _ts("2026-03-10 14:00")
    day2 = _ts("2026-03-11 14:00")

    first = eng.on_signal(_entry(day1), Quote(bid=1.06, ask=1.07), now=day1)
    assert len(first) == 1

    second = eng.on_signal(_entry(day2, bid=1.00), Quote(bid=1.01, ask=1.02), now=day2)
    assert len(second) == 1, f"re-entry was refused: {_reasons(eng)}"
    assert second[0].qty > 0
    assert eng.positions["QQQ|CALL|719|0DTE"].contracts == second[0].qty
    assert any(f.event == "expire" for f in eng.fills.records)


def test_expired_position_does_not_hold_a_concurrency_slot():
    eng = _engine()
    day1 = _ts("2026-03-10 14:00")
    day2 = _ts("2026-03-11 14:00")

    eng.on_signal(_entry(day1), Quote(bid=1.06, ask=1.07), now=day1)
    eng.on_signal(_entry(day1, ticker="SPY", strike=700.0), Quote(1.0, 1.0), now=day1)
    assert len(eng.open_positions) == 2

    intents = eng.on_signal(_entry(day2, ticker="NVDA", strike=180.0),
                            Quote(1.0, 1.0), now=day2)
    assert len(intents) == 1, f"expired book still blocked the day: {_reasons(eng)}"


def test_stop_blocks_adds_but_still_allows_an_exit():
    eng = _engine()
    ts = _ts("2026-03-10 10:00")
    eng.on_signal(_entry(ts), Quote(bid=1.06, ask=1.07), now=ts)
    held = eng.positions["QQQ|CALL|719|0DTE"].contracts

    eng.on_signal(Signal(kind=AlertKind.STOP, channel=ET, raw="STAND DOWN"), None, now=ts)
    assert eng.halted is True

    add = eng.on_signal(_add(ts + 30), Quote(bid=1.11, ask=1.12), now=ts + 30)
    assert add == []
    assert eng.positions["QQQ|CALL|719|0DTE"].contracts == held
    assert "halted:stand-down" in _reasons(eng)

    close = Signal(kind=AlertKind.CLOSE, channel=ET, ticker="QQQ", side=Side.CALL,
                   strike=719.0, expiry="0DTE", price=1.50, raw="close")
    assert len(eng.on_signal(close, Quote(bid=1.49, ask=1.51), now=ts + 60)) == 1


def test_add_cannot_bypass_a_stand_down_governor():
    """The policy is reduce-only, and adds are part of the clip it governs."""
    state = {"factor": 1.0}

    def governor(_state, _signal, _ts):
        return Decision(factor=state["factor"], stand_down=state["factor"] <= 0,
                        reasons=("test",))

    eng = Engine(config=EngineConfig(pipe_test=False), fills=FillLog(), governor=governor)
    ts = _ts("2026-03-10 10:00")
    eng.on_signal(_entry(ts), Quote(bid=1.06, ask=1.07), now=ts)
    before = eng.positions["QQQ|CALL|719|0DTE"].contracts

    state["factor"] = 0.0
    add = eng.on_signal(_add(ts + 30), Quote(bid=1.11, ask=1.12), now=ts + 30)
    assert add == []
    assert eng.positions["QQQ|CALL|719|0DTE"].contracts == before
    assert any("governor" in reason for reason in _reasons(eng))


def test_a_named_unheld_contract_cannot_resolve_to_another_strike():
    """A close for QQQ 720C must never close the held QQQ 719C."""
    eng = _engine()
    ts = _ts("2026-03-10 10:00")
    eng.on_signal(_entry(ts, strike=719.0), Quote(1.0, 1.0), now=ts)
    held = eng.positions["QQQ|CALL|719|0DTE"].contracts

    close_720 = Signal(kind=AlertKind.CLOSE, channel=ET, ticker="QQQ", side=Side.CALL,
                       strike=720.0, expiry="0DTE", price=1.2, raw="close 720")
    intents = eng.on_signal(close_720, Quote(1.19, 1.21), now=ts + 30)

    assert intents == []
    assert eng.positions["QQQ|CALL|719|0DTE"].contracts == held
    assert "QQQ|CALL|720|0DTE" not in eng.positions
    assert "close_without_position" in _reasons(eng)


def test_a_named_unheld_contract_cannot_receive_an_add():
    eng = _engine()
    ts = _ts("2026-03-10 10:00")
    eng.on_signal(_entry(ts, strike=719.0), Quote(1.0, 1.0), now=ts)
    held = eng.positions["QQQ|CALL|719|0DTE"].contracts

    add_720 = _add(ts + 30, strike=720.0, price=1.2)
    assert eng.on_signal(add_720, Quote(1.19, 1.21), now=ts + 30) == []
    assert eng.positions["QQQ|CALL|719|0DTE"].contracts == held
    assert eng.positions["QQQ|CALL|719|0DTE"].adds == 0


def test_a_close_without_an_expiry_still_resolves_the_held_strike():
    """A real exit may omit the expiry; ticker|side|strike is enough when unique."""
    eng = _engine()
    ts = _ts("2026-03-10 10:00")
    eng.on_signal(_entry(ts, strike=719.0), Quote(1.0, 1.0), now=ts)

    close = Signal(kind=AlertKind.CLOSE, channel=ET, ticker="QQQ", side=Side.CALL,
                   strike=719.0, expiry="", price=1.2, raw="QQQ 719 calls closed")
    intents = eng.on_signal(close, Quote(1.19, 1.21), now=ts + 30)
    assert len(intents) == 1
    assert intents[0].contract_key == "QQQ|CALL|719|0DTE"


def test_a_close_for_a_different_known_expiry_does_not_close_the_held_one():
    eng = _engine()
    ts = _ts("2026-03-10 10:00")
    eng.on_signal(_entry(ts, strike=719.0), Quote(1.0, 1.0), now=ts)

    close = Signal(kind=AlertKind.CLOSE, channel=ET, ticker="QQQ", side=Side.CALL,
                   strike=719.0, expiry="2026-09-18", price=1.2, raw="close Sep18")
    assert eng.on_signal(close, Quote(1.19, 1.21), now=ts + 30) == []
    assert eng.positions["QQQ|CALL|719|0DTE"].is_open


def test_add_governor_sees_the_position_channel():
    """A channel tilt must apply to an add that omits the channel."""
    seen: list[str] = []

    def governor(_state, signal, _ts):
        seen.append(signal.channel)
        return Decision(factor=1.0, stand_down=False, reasons=("test",))

    eng = Engine(config=EngineConfig(pipe_test=False), fills=FillLog(), governor=governor)
    ts = _ts("2026-03-10 10:00")
    eng.on_signal(_entry(ts, channel=HIGH), Quote(bid=1.00, ask=1.00), now=ts)
    eng.on_signal(_add(ts + 30, channel="", price=1.00), Quote(1.0, 1.0), now=ts + 30)
    assert seen[-1] == HIGH


def test_add_uses_the_position_channel_for_the_clip():
    """An add post without a channel is still a high-confidence add."""
    eng = _engine()
    ts = _ts("2026-03-10 10:00")
    eng.on_signal(_entry(ts, channel=HIGH), Quote(bid=1.00, ask=1.00), now=ts)

    # The add post names the contract but not the channel.
    add = _add(ts + 30, channel="", price=1.00)
    intents = eng.on_signal(add, Quote(bid=1.00, ask=1.00), now=ts + 30)

    assert len(intents) == 1
    # high-confidence ADD_CLIP_USD = $4500 / ($1.00 * 100) = 45 contracts.
    # Falling back to the ticker produced the standard 22-contract add.
    assert intents[0].qty == 45
    assert "high_confidence" in intents[0].reason

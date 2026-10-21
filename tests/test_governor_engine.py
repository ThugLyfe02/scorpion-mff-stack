"""The risk layer wired into the engine, end to end.

`test_risk_policy.py` proves the arithmetic. This proves the wiring: the engine asks
the governor, honours a reduction, records a stand-down with its reason, survives a
restart with its drawdown intact, and cannot be argued into sizing up.
"""

from __future__ import annotations

import time

import pytest

from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.governor import GovernorState, decide
from scorpion_mff.parser import parse
from scorpion_mff.policy import baseline
from scorpion_mff.types import Quote, RawMessage

KING = "options-king"
KING_ID = "968352649437126676"
DAY = 1_757_000_000.0
DAY2 = DAY + 86_400.0


def raw(text: str, ts: float = DAY) -> RawMessage:
    return RawMessage(channel=KING, channel_id=KING_ID, text=text, ts=ts)


DAY_NAME = time.strftime("%Y-%m-%d", time.gmtime(DAY))


class FixedQuotes:
    def __init__(self, bid: float = 1.0, ask: float = 1.0):
        self.bid, self.ask = bid, ask

    def quote(self, key: str):
        return Quote(self.bid, self.ask)


def entry(ticker: str = "QQQ", strike: str = "600", ts: float = DAY) -> RawMessage:
    return raw(f"CALL TICKER {ticker} STRIKE ${strike} Exp 0DTE Current BID Price $1.00", ts=ts)


def engine(*, policy=None, state: GovernorState | None = None, path=None) -> Engine:
    p = policy or baseline(name="test")
    return Engine(
        config=EngineConfig(pipe_test=False),          # full clips: sizing is visible
        fills=FillLog(), quotes=FixedQuotes(),
        governor=lambda st, sig, ts: decide(p, st, channel=sig.channel, ts=ts),
        risk_state=state or GovernorState(), risk_state_path=path,
    )


def test_the_engine_sizes_at_the_locked_clip_when_nothing_binds():
    eng = engine()
    intents = eng.on_signal(parse(entry()), now=DAY)
    assert intents, "a clean entry must trade"
    # $1500 standard clip at a $1.00 limit = 15 contracts
    assert intents[0].qty == 15


def test_a_halving_policy_halves_the_clip():
    p = baseline(name="half", channel_multipliers={"options-king": 0.5})
    eng = engine(policy=p)
    intents = eng.on_signal(parse(entry()), now=DAY)
    assert intents[0].qty == 7, "15 * 0.5 floors to 7, never rounds up"


def test_a_stand_down_skips_with_an_auditable_reason():
    state = GovernorState()
    state.record_result(-4_000.0, DAY_NAME)       # -2.67R: past the daily stop
    eng = engine(state=state)
    intents = eng.on_signal(parse(entry(ts=DAY)), now=DAY)
    assert intents == []
    last = eng.fills.records[-1]
    assert last.event == "skip" and last.reason.startswith("governor:")
    assert "daily_stop" in last.reason


def test_the_engine_cannot_be_talked_into_sizing_up():
    p = baseline(name="greedy", channel_multipliers={"options-king": 1.0})
    eng = engine(policy=p)
    eng.governor = lambda st, sig, ts: type("D", (), {"factor": 5.0, "stand_down": False,
                                                     "reasons": (), "reason_text": "x"})()
    intents = eng.on_signal(parse(entry()), now=DAY)
    assert intents[0].qty == 15, "apply_contracts clamps even outside the policy path"


def test_a_loss_moves_the_risk_state():
    eng = engine()
    eng.on_signal(parse(entry()), now=DAY)
    eng.on_signal(parse(raw("QQQ calls closed at 0.50", ts=DAY + 600)), now=DAY + 600)
    # 15 contracts bought at 1.00, sold at 0.50 -> -$750
    assert eng.stats.realized_usd == pytest.approx(-750.0)
    assert eng.governor_state.consecutive_losses == 1
    assert eng.governor_state.equity_usd == pytest.approx(-750.0)


def test_risk_state_is_persisted_after_a_settle(tmp_path):
    path = tmp_path / "governor.json"
    eng = engine(path=path)
    eng.on_signal(parse(entry()), now=DAY)
    eng.on_signal(parse(raw("QQQ calls closed at 0.50", ts=DAY + 600)), now=DAY + 600)
    assert path.exists(), "a settle must write the risk file"
    revived = GovernorState.load(path)
    assert revived.equity_usd == pytest.approx(-750.0)


def test_a_relaunched_engine_inherits_a_stand_down(tmp_path):
    """The property that makes persistence worth having: a crash loop cannot reset risk."""
    state = GovernorState()
    state.record_result(-9_500.0, DAY_NAME)
    decide(baseline(name="t"), state, channel=KING, ts=DAY)   # sets halted_reason
    assert state.halted_reason

    revived = GovernorState.from_dict(state.to_dict())
    eng = engine(state=revived)
    intents = eng.on_signal(parse(entry()), now=DAY)
    assert intents == [], "a relaunched process must not trade through a stand-down"
    assert "stand-down" in eng.fills.records[-1].reason


def test_a_writable_risk_file_failure_is_logged_not_fatal(tmp_path, monkeypatch):
    eng = engine(path=tmp_path / "nope" / "deep" / "governor.json")
    monkeypatch.setattr(GovernorState, "save",
                        lambda self, p: (_ for _ in ()).throw(OSError("disk full")))
    eng.on_signal(parse(entry()), now=DAY)
    eng.on_signal(parse(raw("QQQ calls closed at 1.50", ts=DAY + 600)), now=DAY + 600)
    reasons = [f.reason for f in eng.fills.records if f.event == "alert"]
    assert any(r.startswith("risk_state_unwritable") for r in reasons)
    assert eng.stats.realized_usd > 0, "a bookkeeping failure must not swallow the P&L"

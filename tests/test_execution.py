"""Execution safety tests: the circuit breaker and the opt-in spread gate.

These are the PR#1 breaker semantics ported into the MFF hot path, plus the
proofs the task asked for:

  * threshold / cooldown / one probe, on an injected clock
  * exits stay placeable while the breaker is open (kill-switch invariant)
  * a business reject does NOT trip the breaker
  * the spread gate boundary, and OFF-by-default replay parity
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scorpion_mff.broker import BrokerError, PaperBroker, QuoteScript, TransportError
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.execution import CircuitBreaker, spread_ok, spread_ratio
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.parser import parse
from scorpion_mff.replay import replay_file
from scorpion_mff.types import OrderIntent, Quote, RawMessage

ROOT = Path(__file__).resolve().parent.parent
TAPE = ROOT / "tapes" / "sep8.json"
QQQ = "QQQ|CALL|719|0DTE"


def make_listener(broker, breaker, tmp_path) -> Listener:
    """A minimal listener whose _place() can be driven directly, on a fixed clock."""
    return Listener(
        transport=TapeTransport([]),
        engine=Engine(config=EngineConfig(), fills=FillLog(), clock=lambda: 1000.0),
        broker=broker,
        quotes=QuoteScript({}),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=lambda: 1000.0),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: 1000.0),
        clock=lambda: 1000.0,
        breaker=breaker,
    )


def buy_intent(key: str = QQQ) -> OrderIntent:
    return OrderIntent(action="buy", qty=1, limit=1.05, contract_key=key,
                       reason="entry:pipe", idempotency_key=f"{key}:buy:1")


def sell_intent(key: str = QQQ) -> OrderIntent:
    return OrderIntent(action="sell", qty=1, limit=1.05, contract_key=key,
                       reason="close", idempotency_key=f"{key}:close")


# ── the breaker: threshold / cooldown / one probe ────────────────────────────
def test_breaker_opens_after_the_threshold_and_stays_open_for_the_cooldown():
    b = CircuitBreaker(failure_threshold=3, cooldown_seconds=30, clock=lambda: 1000.0)
    assert b.allow(1000.0) is True
    b.record_failure(1000.0)
    b.record_failure(1001.0)
    assert b.is_open is False
    b.record_failure(1002.0)
    assert b.is_open is True
    assert b.allow(1003.0) is False
    assert b.allow(1029.0) is False       # still inside the cooldown


def test_breaker_permits_exactly_one_half_open_probe():
    b = CircuitBreaker(failure_threshold=2, cooldown_seconds=30, clock=lambda: 1000.0)
    b.record_failure(1000.0)
    b.record_failure(1000.0)
    assert b.is_open is True
    assert b.allow(1030.0) is True        # half-open: the one probe may pass
    assert b.allow(1030.0) is False       # ... and only one
    assert b.allow(1031.0) is False


def test_probe_success_closes_and_probe_failure_reopens():
    good = CircuitBreaker(failure_threshold=2, cooldown_seconds=10, clock=lambda: 0.0)
    good.record_failure(0.0)
    good.record_failure(0.0)
    assert good.allow(10.0) is True       # half-open
    good.record_success()
    assert good.is_open is False
    assert good.failures == 0

    bad = CircuitBreaker(failure_threshold=2, cooldown_seconds=10, clock=lambda: 0.0)
    bad.record_failure(0.0)
    bad.record_failure(0.0)
    assert bad.allow(10.0) is True        # half-open probe
    bad.record_failure(10.0)              # the probe fails
    assert bad.is_open is True
    assert bad.allow(10.0) is False       # full cooldown again
    assert bad.allow(20.0) is True        # a fresh probe after the re-open cooldown


def test_breaker_uses_the_injected_clock_deterministically():
    ticks = [1000.0]
    b = CircuitBreaker(failure_threshold=1, cooldown_seconds=5, clock=lambda: ticks[0])
    b.record_failure()                    # no argument -> injected clock
    assert b.opened_until == 1005.0
    ticks[0] = 1004.0
    assert b.allow() is False
    ticks[0] = 1005.0
    assert b.allow() is True              # half-open at exactly the boundary


# ── the spread gate ──────────────────────────────────────────────────────────
def test_spread_gate_is_off_by_default_and_the_boundary_is_exact():
    assert spread_ok(Quote(bid=1.0, ask=100.0), None) is True   # off -> always pass
    q = Quote(bid=1.00, ask=1.10)
    assert spread_ratio(q) == pytest.approx(0.10)
    assert spread_ok(q, 0.10) is True      # exactly the ratio is inside
    assert spread_ok(q, 0.0999) is False   # anything wider is refused


def test_spread_gate_fails_closed_on_an_unpriceable_bid():
    assert spread_ok(Quote(bid=0.0, ask=1.0), 0.10) is False
    assert spread_ratio(Quote(bid=0.0, ask=1.0)) is None


def test_engine_skips_with_the_named_spread_reason_only_when_armed():
    msg = RawMessage(channel="etf-options", channel_id="1231301953972207667", ts=1000.0,
                     text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.00")
    sig = parse(msg)

    off = Engine(config=EngineConfig(max_spread_ratio=None, pipe_test=False),
                 fills=FillLog(), clock=lambda: 1000.0)
    assert len(off.on_signal(sig, Quote(bid=1.00, ask=1.20), now=1000.0)) == 1

    on = Engine(config=EngineConfig(max_spread_ratio=0.10, pipe_test=False),
                fills=FillLog(), clock=lambda: 1000.0)
    assert len(on.on_signal(sig, Quote(bid=1.00, ask=1.10), now=1000.0)) == 1   # boundary

    wide = Engine(config=EngineConfig(max_spread_ratio=0.10, pipe_test=False),
                  fills=FillLog(), clock=lambda: 1000.0)
    assert wide.on_signal(sig, Quote(bid=1.00, ask=1.11), now=1000.0) == []
    assert any(f.reason.startswith("spread_too_wide")
               for f in wide.fills.records if not f.accepted)


def test_max_spread_ratio_reads_the_environment(monkeypatch):
    monkeypatch.delenv("MFF_MAX_SPREAD_RATIO", raising=False)
    assert EngineConfig().max_spread_ratio is None
    monkeypatch.setenv("MFF_MAX_SPREAD_RATIO", "0.10")
    assert EngineConfig().max_spread_ratio == 0.10


# ── the listener: breaker wiring ─────────────────────────────────────────────
def test_transport_failures_open_the_breaker_and_page_once(tmp_path):
    class DeadVenue:
        def place(self, intent):
            raise TransportError("transport error: connection refused")

    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30,
                             clock=lambda: 1000.0)
    listener = make_listener(DeadVenue(), breaker, tmp_path)

    listener._place(buy_intent())
    assert breaker.is_open is False
    listener._place(buy_intent())
    assert breaker.is_open is True

    alerts = [f for f in listener.fills.records if f.event == "alert"]
    assert len(alerts) == 1
    assert alerts[0].reason == "breaker_open"

    listener._place(buy_intent())          # still open: blocked, not a second page
    assert len([f for f in listener.fills.records if f.event == "alert"]) == 1


def test_exits_still_place_when_the_breaker_is_open(tmp_path):
    broker = PaperBroker()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30,
                             clock=lambda: 1000.0)
    listener = make_listener(broker, breaker, tmp_path)
    breaker.record_failure(1000.0)         # open immediately
    assert breaker.is_open is True

    listener._place(buy_intent())
    listener._place(sell_intent())

    # the buy was refused before the venue; the exit reached the venue
    assert len(broker.acks) == 1
    assert broker.acks[0].contract_key == QQQ
    assert any(f.event == "reject" and f.reason == "breaker_open"
               for f in listener.fills.records)
    assert any(f.event == "ack" for f in listener.fills.records)


def test_a_business_reject_does_not_trip_the_breaker(tmp_path):
    class VenueRejects:
        def place(self, intent):
            raise BrokerError("insufficient buying power")

    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30,
                             clock=lambda: 1000.0)
    listener = make_listener(VenueRejects(), breaker, tmp_path)

    listener._place(buy_intent())
    listener._place(buy_intent())
    assert breaker.is_open is False
    assert breaker.failures == 0
    assert all(f.reason.startswith("broker:") for f in listener.fills.records
               if f.event == "reject")


def test_a_business_reject_breaks_a_transport_failure_streak(tmp_path):
    """Consecutive means consecutive: a venue answer in between resets the run."""
    class Flaky:
        def __init__(self):
            self.calls = 0

        def place(self, intent):
            self.calls += 1
            if self.calls in (1, 3):
                raise TransportError("transport error: timeout")
            raise BrokerError("insufficient buying power")

    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=30,
                             clock=lambda: 1000.0)
    listener = make_listener(Flaky(), breaker, tmp_path)

    for _ in range(3):
        listener._place(buy_intent())
    assert breaker.is_open is False        # transport, reject, transport = 1 after reset


# ── replay parity ────────────────────────────────────────────────────────────
def test_replay_unchanged_with_the_spread_gate_off_by_default():
    assert EngineConfig().max_spread_ratio is None
    report = replay_file(TAPE)
    assert report.events == 4
    assert report.buys == 2 and report.sells == 2
    assert report.realized_usd == pytest.approx(211.00, abs=0.01)

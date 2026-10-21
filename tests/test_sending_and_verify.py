"""Sending-state discipline, the circuit breaker, and replay verification.

Three properties, each about a way a live system quietly lies:

1. an ambiguous send must never be retried blind (that is how one order becomes two);
2. a failing venue must stop being hammered before the queue becomes dead letters;
3. the book must equal its own log, or the desk stops.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scorpion.config import Policy
from scorpion.outbox import Breaker, Dispatcher, Receipt
from scorpion.paper import quote
from scorpion.pipeline import Pipeline
from scorpion.scenario import message
from scorpion.store import Store
from scorpion.verify import replay_applied, verify, verify_and_halt

D = Decimal
START = datetime.now(UTC).replace(microsecond=0)
KEY = f"QQQ|CALL|719|{START.date().isoformat()}"


def soon() -> datetime:
    """Dispatch runs at wall-clock now: a module-level constant goes stale mid-suite."""
    return datetime.now(UTC) + timedelta(seconds=1)


def store(tmp_path) -> Store:
    return Store(tmp_path / "s.db")


class Boom:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, envelope, *, now):  # noqa: ANN001
        self.calls += 1
        raise TimeoutError("venue did not answer")


class Ok:
    def send(self, envelope, *, now):  # noqa: ANN001
        return Receipt(ok=True, broker_ref="ord-1", fill_price=D("1.02"), fill_qty=1)


def pipeline_with_entry(s: Store, tmp_path) -> Pipeline:
    p = Pipeline(store=s)
    asyncio.run(p.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    return p


def quotes(at: datetime) -> dict:
    return {KEY: quote("1.00", "1.02", at=at)}


# ── ambiguous sends are quarantined, never retried ─────────────────────────
def test_a_transport_exception_marks_the_effect_unknown_not_retry(tmp_path):
    s = store(tmp_path)
    p = pipeline_with_entry(s, tmp_path)
    dispatcher = Dispatcher(store=s, sender=Boom(), policy=Policy(live_submission_enabled=True))
    moment = soon()          # one clock per decision: a quote stamped after `now` is refused
    report = dispatcher.dispatch_once(state=p.state, now=moment, quotes=quotes(moment))
    assert report.items[0].outcome == "UNKNOWN"
    assert s.outbox_snapshot()["counts"] == {"UNKNOWN": 1}
    unknown = s.load_unknown()
    assert len(unknown) == 1 and "TimeoutError" in str(unknown[0]["last_error"])


def test_an_unknown_effect_is_not_claimed_again(tmp_path):
    s = store(tmp_path)
    p = pipeline_with_entry(s, tmp_path)
    t = soon()
    Dispatcher(store=s, sender=Boom(), policy=Policy(live_submission_enabled=True)).dispatch_once(
        state=p.state, now=t, quotes=quotes(t)
    )
    second = Dispatcher(store=s, sender=Ok(), policy=Policy(live_submission_enabled=True))
    assert second.dispatch_once(state=p.state, now=t + timedelta(seconds=60), quotes=quotes(t + timedelta(seconds=60))).attempted == 0


def test_a_crash_while_sending_becomes_unknown_on_recovery(tmp_path):
    s = store(tmp_path)
    pipeline_with_entry(s, tmp_path)
    # claim it, then never mark it: that is exactly a process dying mid-send
    claimed = s.claim_due(owner="worker", now=soon(), limit=10, lease_seconds=30)
    assert len(claimed) == 1
    recovered = s.recover_sending(now=datetime.now(UTC) + timedelta(seconds=90))
    assert len(recovered) == 1
    assert s.outbox_snapshot()["counts"] == {"UNKNOWN": 1}


def test_reconciliation_resolves_an_unknown_either_way(tmp_path):
    s = store(tmp_path)
    pipeline_with_entry(s, tmp_path)
    effect_id = s.claim_due(owner="w", now=soon(), limit=10, lease_seconds=30)[0]["effect_id"]
    s.mark_unknown(effect_id=int(str(effect_id)), reason="timeout", now=datetime.now(UTC))
    assert s.resolve_unknown(effect_id=int(str(effect_id)), sent=True, broker_ref="ord-9",
                             now=datetime.now(UTC)) == "SENT"
    assert s.outbox_snapshot()["counts"] == {"SENT": 1}


# ── the breaker ────────────────────────────────────────────────────────────
def test_the_breaker_opens_after_consecutive_failures_and_recloses():
    breaker = Breaker(failure_threshold=2, cooldown_seconds=30)
    now = START
    assert breaker.allow(now)
    breaker.record_failure(now)
    assert breaker.allow(now), "one failure is not a pattern"
    breaker.record_failure(now)
    assert not breaker.allow(now + timedelta(seconds=1))
    assert breaker.allow(now + timedelta(seconds=31)), "cooldown expires into a probe"
    breaker.record_success()
    assert breaker.failures == 0 and not breaker.is_open


def test_an_open_breaker_short_circuits_the_whole_pass(tmp_path):
    s = store(tmp_path)
    p = pipeline_with_entry(s, tmp_path)
    breaker = Breaker(failure_threshold=1, cooldown_seconds=60)
    dispatcher = Dispatcher(
        store=s, sender=Boom(), policy=Policy(live_submission_enabled=True), breaker=breaker
    )
    t = soon()
    dispatcher.dispatch_once(state=p.state, now=t, quotes=quotes(t))
    blocked = dispatcher.dispatch_once(state=p.state, now=t + timedelta(seconds=5),
                                       quotes=quotes(t + timedelta(seconds=5)))
    assert blocked.attempted == 0
    assert blocked.items[0].outcome == "CIRCUIT_OPEN"


# ── replay verification ────────────────────────────────────────────────────
def test_a_book_rebuilt_from_its_log_matches_after_an_entry_and_a_fill(tmp_path):
    s = store(tmp_path)
    p = pipeline_with_entry(s, tmp_path)
    t = soon()
    Dispatcher(store=s, sender=Ok(), policy=Policy(live_submission_enabled=True)).dispatch_once(
        state=p.state, now=t, quotes=quotes(t)
    )
    # settle the fill into the live book, exactly as the dispatcher hook does
    item = s.load_executions()
    assert len(item) == 1
    p.settle(contract_key=KEY, generation=1, quantity_delta=1, fill_price=D("1.02"),
             market_day=START.date())

    result = verify(s, p.state)
    assert result.ok, [d.describe() for d in result.divergences]
    assert result.fills == 1 and result.signals == 1


def test_the_verifier_detects_a_divergent_book_and_halts(tmp_path):
    s = store(tmp_path)
    p = pipeline_with_entry(s, tmp_path)
    p.settle(contract_key=KEY, generation=1, quantity_delta=1, fill_price=D("1.02"),
             market_day=START.date())
    drifted = replace(
        p.state,
        positions={KEY: replace(p.state.positions[KEY], quantity=99)},
    )
    result = verify_and_halt(s, drifted)
    assert not result.ok
    assert any(d.field.endswith(".quantity") for d in result.divergences)
    assert s.halt_state()[0] is True


def test_a_missing_fill_is_visible_as_a_quantity_divergence(tmp_path):
    """Without fills in the log, a replay would rebuild quantity 0 and call it agreement."""
    s = store(tmp_path)
    p = pipeline_with_entry(s, tmp_path)
    p.settle(contract_key=KEY, generation=1, quantity_delta=1, fill_price=D("1.02"),
             market_day=START.date())
    rows = [r for r in s.load_applied() if r["kind"] == "signal"]   # drop the fill
    replayed = replay_applied(rows)
    assert replayed.positions[KEY].quantity == 0
    assert p.state.positions[KEY].quantity == 1


def test_an_unknown_applied_kind_is_rejected():
    with pytest.raises(ValueError):
        replay_applied([{"seq": 1, "kind": "mystery", "contract_key": None,
                         "payload_json": json.dumps({}), "ts_utc": START.isoformat()}])


def test_the_scenario_proves_its_book_equals_its_log(tmp_path):
    from scorpion.config import Policy as P
    from scorpion.scenario import demo_messages, demo_quotes, run

    result = run(store=store(tmp_path), messages=demo_messages(START),
                 quotes=demo_quotes(), policy=P(live_submission_enabled=True))
    assert result.verified is True
    assert result.verification["ok"] is True


def test_log_integrity_flags_a_signal_that_was_never_applied(tmp_path):
    from scorpion.parser import parse_message
    from scorpion.verify import verify_log

    s = store(tmp_path)
    asyncio.run(Pipeline(store=s).handle(
        message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    # persist a second signal without applying it - exactly a crash between the two writes
    event = parse_message(message("SPY 600P TODAY @ 0.90", message_id="m2", at=START))
    s.append_signal(event)

    result = verify_log(s)
    assert not result.ok
    assert any("signal_not_applied" in d.field for d in result.divergences)


def test_verification_reports_counts(tmp_path):
    s = store(tmp_path)
    p = pipeline_with_entry(s, tmp_path)
    result = verify(s, p.state)
    assert result.ok and result.applied_seq == 1 and result.positions == 1
    assert s.applied_counts() == {"signal": 1}

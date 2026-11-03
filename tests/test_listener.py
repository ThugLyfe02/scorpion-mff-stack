"""Listener tests: the pipeline end to end, offline, plus the 9/8 replay proof."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scorpion_mff.broker import PaperBroker, QuoteScript
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.replay import replay_file
from scorpion_mff.types import RawMessage

ROOT = Path(__file__).resolve().parent.parent
TAPE = ROOT / "tapes" / "sep8.json"
QQQ = "QQQ|CALL|719|0DTE"
NVDA = "NVDA|PUT|227.5|2026-09-11"


def build(tape, quotes, tmp_path, **cfg) -> Listener:
    return Listener(
        transport=TapeTransport(tape),
        engine=Engine(config=EngineConfig(**cfg), fills=FillLog(), clock=lambda: 1000.0),
        broker=PaperBroker(),
        quotes=QuoteScript(quotes),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=lambda: 1000.0),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: 1000.0),
        clock=lambda: 1000.0,
    )


def msgs():
    return [
        RawMessage(channel="etf-options", channel_id="1231301953972207667", ts=1000.0,
                   text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01"),
        RawMessage(channel="etf-options", channel_id="1231301953972207667", ts=1360.0,
                   text="Close remaining for 10%"),
    ]


# ── pipeline ─────────────────────────────────────────────────────────────────
def test_one_event_end_to_end_records_intent_ack_and_latency(tmp_path):
    listener = build(msgs()[:1], {QQQ: [(1.01, 1.05)]}, tmp_path)
    intents = listener.handle(RawMessage(channel="etf-options", channel_id="1231301953972207667",
                                         ts=1000.0,
                                         text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01"))
    assert len(intents) == 1 and intents[0].qty == 1
    events = [f.event for f in listener.engine.fills.records]
    assert events == ["buy", "intent", "ack", "latency"]
    # the placement-agnostic buy/sell/ack subsequence is unchanged
    assert [e for e in events if e in {"buy", "sell", "ack"}] == ["buy", "ack"]
    assert listener.processed == 1
    assert listener.breached_budget is False


def test_run_consumes_the_tape_and_stops_at_the_limit(tmp_path):
    listener = build(msgs(), {QQQ: [(1.01, 1.05), (1.11, 1.14)]}, tmp_path)
    processed = asyncio.run(listener.run())
    assert processed == 2
    assert listener.engine.stats.realized_usd == pytest.approx(6.0)   # 1 contract, 1.05 -> 1.11


def test_a_broker_failure_is_logged_as_a_reject_not_a_crash(tmp_path):
    class Exploding:
        def place(self, intent):
            from scorpion_mff.broker import BrokerError
            raise BrokerError("insufficient buying power")

    listener = build(msgs()[:1], {QQQ: [(1.01, 1.05)]}, tmp_path)
    listener.broker = Exploding()
    listener.handle(RawMessage(channel="etf-options", channel_id="1231301953972207667", ts=1000.0,
                               text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01"))
    reasons = [f.reason for f in listener.engine.fills.records if f.event == "reject"]
    assert any("insufficient buying power" in r for r in reasons)


def test_kill_switch_file_blocks_the_live_loop(tmp_path):
    listener = build(msgs(), {QQQ: [(1.01, 1.05)]}, tmp_path)
    listener.kill.stand_down("test")
    listener.handle(RawMessage(channel="etf-options", channel_id="1231301953972207667", ts=1000.0,
                               text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01"))
    assert listener.engine.fills.bought() == []
    assert any("kill-switch" in f.reason for f in listener.engine.fills.records if not f.accepted)


def test_unpriced_contract_fails_closed(tmp_path):
    listener = build(msgs()[:1], {}, tmp_path)       # no quote scripted at all
    listener.handle(RawMessage(channel="etf-options", channel_id="1231301953972207667", ts=1000.0,
                               text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01"))
    assert listener.engine.fills.bought() == []
    assert any(f.reason == "no_quote" for f in listener.engine.fills.records)


# ── the 9/8 proof ────────────────────────────────────────────────────────────
def test_sep8_tape_reproduces_both_missed_trades():
    report = replay_file(TAPE)
    assert report.events == 4
    assert report.buys == 2 and report.sells == 2
    assert report.contracts_bought == 6            # pipe 1 + 5
    assert report.realized_usd == pytest.approx(211.00, abs=0.01)
    assert report.rejected == 0
    assert report.skipped == 0
    assert report.budget["worst_ms"] <= report.budget["budget_ms"]
    assert report.budget["breaches"] == 0


def test_sep8_without_the_pipe_test_reproduces_the_post_mortem_estimate():
    """The $295-315 counterfactual in live/misses is what you get with no pipe test."""
    report = replay_file(TAPE, engine_config=EngineConfig(pipe_test=False))
    assert report.contracts_bought == 19           # 14 + 5
    assert report.realized_usd == pytest.approx(289.00, abs=0.01)
    assert report.realized_usd - 211.00 == pytest.approx(78.00, abs=0.01)


def test_replay_is_deterministic():
    a, b = replay_file(TAPE), replay_file(TAPE)
    assert a.decisions == b.decisions
    assert a.realized_usd == b.realized_usd


def test_the_tape_annotations_match_what_the_pipeline_actually_does():
    """The expected_* blocks in tapes/sep8.json are assertions, not decoration."""
    data = json.loads(TAPE.read_text(encoding="utf-8"))
    lock = replay_file(TAPE)
    no_pipe = replay_file(TAPE, engine_config=EngineConfig(pipe_test=False))
    assert lock.buys == data["expected_with_lock"]["buys"]
    assert lock.contracts_bought == data["expected_with_lock"]["contracts_bought"]
    assert lock.realized_usd == pytest.approx(data["expected_with_lock"]["realized_usd"])
    assert no_pipe.contracts_bought == data["expected_without_pipe_test"]["contracts_bought"]
    assert no_pipe.realized_usd == pytest.approx(data["expected_without_pipe_test"]["realized_usd"])

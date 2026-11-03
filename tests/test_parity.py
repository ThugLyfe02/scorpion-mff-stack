"""Shadow-live parity: the wiring must not change the decision.

The tests come in three layers, and the distinction matters:

* **the clean run** - the sep8 tape through the decision wiring and through the full live
  ladder produces the same intents, the same rows with the same reasons and keys, and the
  same book, event by event;
* **the forced divergences** - a partial, a reject and a transport failure each change the
  book, and the harness must *say so*, with the event, the field and the cause. A parity
  checker that cannot fail is a congratulations machine;
* **the refusals** - an empty tape, a trace from another schema, an impossible venue model
  and a missing event are all exit 2 / a named divergence, never a silent pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scorpion_mff import cli, parity
from scorpion_mff.broker import BrokerError, PaperBroker, QuoteScript, TransportError
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.parity import (
    ShadowBroker,
    Trace,
    VenuePolicy,
    compare,
    compare_modes,
    run_tape,
    state_of,
)
from scorpion_mff.replay import load_quotes, load_tape
from scorpion_mff.types import OrderIntent, RawMessage

ROOT = Path(__file__).resolve().parent.parent
TAPE = ROOT / "tapes" / "sep8.json"
QQQ = "QQQ|CALL|719|0DTE"
NVDA = "NVDA|PUT|227.5|2026-09-11"


@pytest.fixture(scope="module")
def tape() -> list[RawMessage]:
    return load_tape(TAPE)


@pytest.fixture(scope="module")
def quotes() -> dict[str, list[list[float]]]:
    return load_quotes(TAPE)


# ── the clean run: parity is the product ─────────────────────────────────────
def test_the_live_ladder_does_not_change_one_decision_on_the_sep8_tape(tape, quotes):
    report = compare_modes(tape, quotes, tape_name=str(TAPE))

    assert report.ok, report.to_text()
    assert report.events == 4
    assert report.matched == 4
    assert report.reference.summary()["digest"] == report.candidate.summary()["digest"]
    assert report.candidate.summary()["buys"] == 2
    assert report.candidate.summary()["sells"] == 2


def test_the_reference_trace_is_the_pinned_arithmetic(tape, quotes):
    """The reference wiring must agree with the replay oracle, not merely with itself."""
    from scorpion_mff.replay import replay

    result = run_tape(tape, quotes, mode="replay", tape_name=str(TAPE))
    pinned = replay(tape, quotes)

    summary = result.trace.summary()
    assert summary["buys"] == pinned.buys
    assert summary["sells"] == pinned.sells
    assert result.listener.engine.stats.realized_usd == pytest.approx(
        pinned.realized_usd, abs=0.01)


def test_every_event_records_the_intent_reason_key_and_book(tape, quotes):
    result = run_tape(tape, quotes, mode="shadow", tape_name=str(TAPE))
    events = result.trace.events

    assert [event.channel for event in events] == [
        "etf-options", "etf-options", "options-king", "options-king"]
    assert [event.kind for event in events] == ["entry", "close", "entry", "close"]
    # the pipe test is visible in the reason, not inferred from the quantity
    assert "entry:pipe" in events[0].intents[0]
    assert "entry:standard" in events[2].intents[0]
    # the idempotency key travels inside the trace, so a retry is distinguishable
    assert ":buy:" in events[0].intents[0]
    assert ":close:" in events[1].intents[0]
    # and the book is captured after the event, not before it
    assert "realized=0.00" in events[0].state
    assert "realized=6.00" in events[1].state


def test_parity_is_deterministic(tape, quotes):
    first = compare_modes(tape, quotes)
    second = compare_modes(tape, quotes)
    assert first.to_dict() == second.to_dict()


def test_a_price_offset_is_a_venue_difference_and_the_harness_says_so(tape, quotes):
    report = compare_modes(tape, quotes, venue=VenuePolicy(price_offset=0.03))
    assert not report.ok
    assert any("cumulative ack" in row.cause for row in report.divergences)
    assert all(row.cause and row.cause != "unexplained" for row in report.divergences)


# ── the forced divergences: the harness must be able to fail ─────────────────
def test_a_partial_fill_diverges_and_names_the_event_and_the_cause(tape, quotes):
    report = compare_modes(tape, quotes, venue=VenuePolicy(fill_fraction=0.5))

    assert not report.ok
    first = report.divergences[0]
    assert first.index == 2                     # the 5-lot NVDA buy
    assert first.field == "decisions"
    assert "cumulative ack" in first.cause
    # a partial SELL must leave the contract in flight, and that is a third divergence
    assert any(row.field == "state" and "in-flight exit set" in row.cause
               for row in report.divergences)


def test_a_rejected_entry_changes_the_next_exit_decision(tape, quotes):
    """The point of parity: a venue answer is not bookkeeping, it is the next decision."""
    report = compare_modes(tape, quotes, venue=VenuePolicy(reject="position_not_found"))

    assert not report.ok
    reasons = [row.field for row in report.divergences]
    assert reasons.count("intents") == 2        # both exits stop existing
    exit_event = [row for row in report.divergences if row.index == 1][0]
    assert exit_event.reference.startswith("sell|")
    assert exit_event.candidate == "(empty)" or exit_event.candidate == ""
    assert "refused the order" in exit_event.cause


def test_a_transport_failure_keeps_the_exit_in_flight_rather_than_claiming_flat(tape, quotes):
    report = compare_modes(tape, quotes, venue=VenuePolicy(transport="timeout"))
    assert not report.ok
    final = report.candidate.events[-1].state
    assert f"inflight[{QQQ},{NVDA}]" in final or f"inflight[{NVDA},{QQQ}]" in final
    assert any("did not answer" in row.cause for row in report.divergences)


# ── the trace is an artefact ─────────────────────────────────────────────────
def test_a_trace_round_trips_through_json(tape, quotes, tmp_path):
    result = run_tape(tape, quotes, mode="shadow", tape_name=str(TAPE))
    target = result.trace.save(tmp_path / "trace.json")
    loaded = Trace.load(target)

    assert loaded.mode == "shadow"
    assert loaded.tape == str(TAPE)
    assert loaded.summary() == result.trace.summary()
    assert compare(loaded, result.trace).ok


def test_a_trace_from_another_schema_is_refused_not_guessed(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"schema": 99, "events": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        Trace.load(path)


def test_a_non_json_trace_is_refused(tmp_path):
    path = tmp_path / "torn.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not JSON"):
        Trace.load(path)


def test_save_is_atomic_and_leaves_no_scratch_file(tape, quotes, tmp_path):
    result = run_tape(tape, quotes, mode="replay")
    target = result.trace.save(tmp_path / "nested" / "trace.json")
    assert target.exists()
    assert list(target.parent.iterdir()) == [target]


# ── the comparison itself ────────────────────────────────────────────────────
def test_a_missing_event_is_a_divergence_not_a_pass(tape, quotes):
    result = run_tape(tape, quotes, mode="shadow")
    truncated = Trace(mode="shadow")
    truncated.events = list(result.trace.events[:-1])

    report = compare(result.trace, truncated)
    assert not report.ok
    assert report.divergences[-1].field == "events"
    assert "lost or invented an event" in report.divergences[-1].cause


def test_the_comparison_does_not_stop_at_the_first_difference(tape, quotes):
    full = run_tape(tape, quotes, mode="shadow")
    broken = Trace(mode="shadow")
    broken.events = [
        parity.EventTrace(**{**event.__dict__, "state": "pos[] realized=0.00"})
        for event in full.trace.events
    ]
    report = compare(full.trace, broken)
    assert len(report.divergences) >= 3
    assert len({row.index for row in report.divergences}) >= 3


def test_state_of_captures_the_guards_not_only_the_positions():
    engine = Engine(fills=FillLog())
    engine.halted = True
    engine.halt_reason = "reconcile-drift"
    engine.exit_in_flight = {"A|CALL|1|0DTE"}
    text = state_of(engine)
    assert "halted=1:reconcile-drift" in text
    assert "inflight[A|CALL|1|0DTE]" in text


# ── the shadow venue is a model, not a client ────────────────────────────────
def test_the_shadow_broker_cannot_be_pointed_at_a_venue():
    broker = ShadowBroker()
    for forbidden in ("base_url", "token", "transport", "retries", "timeout_s", "dry_run"):
        assert not hasattr(broker, forbidden), (
            f"ShadowBroker.{forbidden} would make it a live broker with a flag")


def test_the_shadow_broker_answers_deterministically():
    broker = ShadowBroker()
    intent = OrderIntent(action="buy", qty=2, limit=1.05, contract_key=QQQ,
                         reason="entry:test", idempotency_key=f"{QQQ}:buy:1")
    ack = broker.place(intent)
    assert (ack.ok, ack.qty, ack.price) == (True, 2, 1.05)
    assert broker.lookup(intent.idempotency_key) == "seen"
    assert broker.lookup("never-sent") == "not_seen"


def test_the_shadow_broker_refuses_an_impossible_order():
    broker = ShadowBroker()
    with pytest.raises(BrokerError):
        broker.place(OrderIntent(action="buy", qty=0, limit=1.0, contract_key=QQQ,
                                 reason="x", idempotency_key="k"))


def test_the_shadow_broker_models_a_transport_failure_as_transport():
    broker = ShadowBroker(policy=VenuePolicy(transport="connection reset"))
    with pytest.raises(TransportError):
        broker.place(OrderIntent(action="sell", qty=1, limit=1.0, contract_key=QQQ,
                                 reason="x", idempotency_key="k"))


@pytest.mark.parametrize("kwargs", [
    {"fill_fraction": 0.0},
    {"fill_fraction": 1.5},
    {"reject": "no", "transport": "no"},
])
def test_an_impossible_venue_policy_is_refused(kwargs):
    with pytest.raises(ValueError):
        VenuePolicy(**kwargs)


def test_an_unknown_wiring_is_refused(tape, quotes):
    with pytest.raises(ValueError, match="mode"):
        run_tape(tape, quotes, mode="maybe")


# ── the listener hook ────────────────────────────────────────────────────────
def build_listener(tmp_path, trace=None) -> Listener:
    return Listener(
        transport=TapeTransport([]),
        engine=Engine(config=EngineConfig(), fills=FillLog(), clock=lambda: 1000.0),
        broker=PaperBroker(),
        quotes=QuoteScript({QQQ: [(1.01, 1.05)]}),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=lambda: 1000.0),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: 1000.0),
        clock=lambda: 1000.0,
        trace=trace,
    )


def test_the_listener_trace_records_and_does_not_participate(tmp_path):
    message = RawMessage(channel="etf-options", channel_id="1231301953972207667",
                         ts=1000.0,
                         text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01")
    plain = build_listener(tmp_path / "a")
    traced = build_listener(tmp_path / "b", trace=Trace(mode="live"))

    intents_plain = plain.handle(message)
    intents_traced = traced.handle(message)

    assert intents_plain == intents_traced
    assert plain.engine.fills.records == traced.engine.fills.records
    assert len(traced.trace.events) == 1
    event = traced.trace.events[0]
    assert event.intents[0].startswith("buy|1|1.05|")
    assert any(row.startswith("ack|") for row in event.decisions)


# ── the CLI contract ─────────────────────────────────────────────────────────
def test_cli_shadow_exits_zero_on_parity(capsys):
    code = cli.main(["shadow", str(TAPE)])
    out = capsys.readouterr().out
    assert code == 0
    assert "PARITY" in out
    assert "venue   fill@limit" in out


def test_cli_shadow_exits_one_on_divergence(capsys):
    code = cli.main(["shadow", str(TAPE), "--venue-reject", "position_not_found"])
    out = capsys.readouterr().out
    assert code == 1
    assert "DIVERGED" in out


def test_cli_shadow_writes_the_pair_a_reviewer_can_diff(tmp_path, capsys):
    code = cli.main(["shadow", str(TAPE), "--trace-dir", str(tmp_path)])
    assert code == 0
    capsys.readouterr()
    assert (tmp_path / "replay.json").exists()
    assert (tmp_path / "shadow.json").exists()

    code = cli.main(["shadow", "--diff", str(tmp_path / "replay.json"),
                     str(tmp_path / "shadow.json")])
    assert code == 0
    assert "PARITY" in capsys.readouterr().out


def test_cli_shadow_diff_of_a_rejected_run_is_exit_one(tmp_path, capsys):
    cli.main(["shadow", str(TAPE), "--venue-reject", "no_buying_power",
              "--trace-dir", str(tmp_path)])
    capsys.readouterr()
    code = cli.main(["shadow", "--diff", str(tmp_path / "replay.json"),
                     str(tmp_path / "shadow.json")])
    assert code == 1
    assert "refused the order" in capsys.readouterr().out


def test_cli_shadow_refuses_an_empty_tape(tmp_path, capsys):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"messages": [], "quotes": {}}), encoding="utf-8")
    code = cli.main(["shadow", str(empty)])
    assert code == 2
    assert "not a pass" in capsys.readouterr().err


def test_cli_shadow_refuses_a_missing_tape(capsys):
    code = cli.main(["shadow", str(ROOT / "tapes" / "does-not-exist.json")])
    assert code == 2


def test_cli_shadow_refuses_an_invalid_venue_model(capsys):
    code = cli.main(["shadow", str(TAPE), "--venue-fill", "2.0"])
    assert code == 2
    assert "cannot shadow" in capsys.readouterr().err


def test_cli_shadow_json_is_machine_readable(capsys):
    code = cli.main(["shadow", str(TAPE), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["schema"] == parity.TRACE_SCHEMA
    assert payload["reference"]["digest"] == payload["candidate"]["digest"]
    assert payload["pinned"]["venue"]["label"] == "fill@limit"

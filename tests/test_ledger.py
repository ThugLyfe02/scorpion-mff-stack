"""Ledger tests: intent -> terminal matching, the crash seam, lookup tri-state,
and the CLI's verify/recover contract (never place, never retry)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from scorpion_mff import cli
from scorpion_mff.broker import (
    OrderLookup,
    PaperBroker,
    QuoteScript,
    RobinhoodAgenticBroker,
    TransportError,
)
from scorpion_mff.cli import cmd_recover, cmd_verify
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.ledger import INTENT, Ledger
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.replay import replay_file
from scorpion_mff.types import Fill, OrderIntent

ROOT = Path(__file__).resolve().parent.parent
TAPE = ROOT / "tapes" / "sep8.json"
QQQ = "QQQ|CALL|719|0DTE"
KEY = f"{QQQ}:buy:1"


def _fill(seq: int, event: str, note: str, *, contract_key: str = QQQ,
          qty: int = 1, price: float = 1.0) -> Fill:
    return Fill(seq=seq, ts=float(seq), event=event, contract_key=contract_key,
                qty=qty, price=price, reason="test", accepted=True,
                latency_ms=None, note=note)


def _intent_only_log(tmp_path: Path) -> Path:
    path = tmp_path / "fills.jsonl"
    FillLog(path=path).record(event="intent", contract_key=QQQ, qty=1, price=1.05,
                              reason="entry:test", ts=1000.0, note=KEY)
    return path


def _two_intent_log(tmp_path: Path) -> Path:
    path = tmp_path / "fills.jsonl"
    log = FillLog(path=path)
    log.record(event="intent", contract_key=QQQ, qty=1, price=1.05,
               reason="entry:seen", ts=1000.0, note="key-seen")
    log.record(event="intent", contract_key=QQQ, qty=2, price=1.06,
               reason="entry:not-sent", ts=1001.0, note="key-not-sent")
    return path


# ── the ledger is pure ───────────────────────────────────────────────────────
def test_unresolved_returns_intents_with_no_later_terminal():
    rows = [_fill(1, "intent", "k1"), _fill(2, "intent", "k2"), _fill(3, "ack", "k2")]
    assert Ledger.unresolved(rows) == [rows[0]]


def test_unresolved_resolves_every_terminal_vocabulary():
    for event in ("ack", "reject", "recovered", "not_sent"):
        rows = [_fill(1, "intent", "k"), _fill(2, event, "k")]
        assert Ledger.unresolved(rows) == [], event


def test_unresolved_requires_the_terminal_to_be_later():
    # a terminal with the same key but an earlier seq cannot close the intent
    rows = [_fill(2, "intent", "k"), _fill(1, "ack", "k")]
    assert Ledger.unresolved(rows) == [rows[0]]


def test_unresolved_ignores_terminals_without_an_intent():
    # the shape of every pre-ledger log: buys, acks and latency, no intents
    rows = [_fill(1, "buy", "etf-options"), _fill(2, "ack", "paper-1"),
            _fill(3, "latency", "")]
    assert Ledger.unresolved(rows) == []


def test_unresolved_preserves_order_and_keys_by_note():
    rows = [_fill(1, "intent", "a"), _fill(2, "intent", "b"),
            _fill(3, "intent", "c"), _fill(4, "recovered", "a")]
    unresolved = Ledger.unresolved(rows)
    assert [f.note for f in unresolved] == ["b", "c"]


# ── the crash seam: intent is written before the broker call ─────────────────
def test_crash_between_intent_and_ack_leaves_the_intent_unresolved(tmp_path):
    class Crashes:
        def place(self, intent):
            raise RuntimeError("simulated hard crash")

    listener = Listener(
        transport=TapeTransport([]),
        engine=Engine(config=EngineConfig(), fills=FillLog(), clock=lambda: 1000.0),
        broker=Crashes(),
        quotes=QuoteScript({}),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=lambda: 1000.0),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: 1000.0),
        clock=lambda: 1000.0,
    )
    intent = OrderIntent(action="buy", qty=1, limit=1.05, contract_key=QQQ,
                         reason="entry:test", idempotency_key=KEY)
    with pytest.raises(RuntimeError):
        listener._place(intent)
    rows = listener.fills.records
    assert [f.event for f in rows] == ["intent"]
    assert rows[0].note == KEY
    assert Ledger.unresolved(rows) == [rows[0]]


# ── a transport failure is not a terminal ───────────────────────────────────
def _listener(broker, tmp_path: Path) -> Listener:
    return Listener(
        transport=TapeTransport([]),
        engine=Engine(config=EngineConfig(), fills=FillLog(), clock=lambda: 1000.0),
        broker=broker,
        quotes=QuoteScript({}),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=lambda: 1000.0),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: 1000.0),
        clock=lambda: 1000.0,
    )


def test_a_transport_failure_quarantines_the_intent_instead_of_closing_it(tmp_path):
    """The order may have reached the venue; a terminal `reject` would erase that."""
    class DeadVenue:
        def place(self, intent):
            raise TransportError("transport error: connection refused")

    listener = _listener(DeadVenue(), tmp_path)
    intent = OrderIntent(action="buy", qty=1, limit=1.05, contract_key=QQQ,
                         reason="entry:test", idempotency_key=KEY)
    listener._place(intent)

    rows = listener.fills.records
    assert [f.event for f in rows] == [INTENT, "transport_unknown"]
    assert rows[1].note == KEY and rows[1].reason.startswith("transport:")
    assert not any(f.event in Ledger.TERMINALS for f in rows)
    assert Ledger.unresolved(rows) == [rows[0]]
    assert Ledger.quarantine_reasons(rows) == {KEY: rows[1].reason}


def test_a_transport_failed_exit_pages_without_inventing_a_terminal(tmp_path):
    """The one asymmetric case: the book is flat, the venue may still hold it."""
    class DeadVenue:
        def place(self, intent):
            raise TransportError("transport error: timeout")

    listener = _listener(DeadVenue(), tmp_path)
    exit_key = "k-exit"
    intent = OrderIntent(action="sell", qty=1, limit=1.11, contract_key=QQQ,
                         reason="close:pct=10%", idempotency_key=exit_key)
    listener._place(intent)

    rows = listener.fills.records
    alerts = [f.reason for f in rows if f.event == "alert"]
    # one failure is below the default threshold, so this is the exit alarm only
    assert alerts == ["exit_unknown:transport"]
    assert any(f.event == "transport_unknown" for f in rows)
    assert not any(f.event in Ledger.TERMINALS for f in rows)
    assert Ledger.unresolved(rows) == [rows[0]]


def test_verify_names_a_transport_unknown_and_recover_settles_it(tmp_path, monkeypatch,
                                                                 capsys):
    path = tmp_path / "fills.jsonl"
    log = FillLog(path=path)
    log.record(event=INTENT, contract_key=QQQ, qty=1, price=1.05,
               reason="entry:test", ts=1000.0, note=KEY)
    log.record(event="transport_unknown", contract_key=QQQ, qty=1, price=1.05,
               reason="transport:timeout", ts=1000.0, accepted=False, note=KEY)

    assert cmd_verify(argparse.Namespace(fills=path)) == 1
    assert "transport unknown: transport:timeout" in capsys.readouterr().out

    stub = StubLookup({KEY: OrderLookup.SEEN})
    monkeypatch.setattr(cli, "make_recover_broker", lambda kind: stub)
    assert cmd_recover(argparse.Namespace(fills=path, broker="robinhood")) == 0

    assert Ledger.unresolved(FillLog().replay_file(path)) == []
    assert cmd_verify(argparse.Namespace(fills=path)) == 0
    assert stub.placed == 0


# ── broker lookup tri-state ──────────────────────────────────────────────────
def test_paper_broker_lookup_is_deterministic():
    broker = PaperBroker()
    assert broker.lookup("k1") is OrderLookup.NOT_SEEN
    broker.place(OrderIntent(action="buy", qty=1, limit=1.0, contract_key=QQQ,
                             reason="r", idempotency_key="k1"))
    assert broker.lookup("k1") is OrderLookup.SEEN
    assert broker.lookup("k2") is OrderLookup.NOT_SEEN


def _rh(**kw) -> RobinhoodAgenticBroker:
    base = dict(lookup_template="http://x/orders/{key}", token="t")
    base.update(kw)
    return RobinhoodAgenticBroker(**base)


def test_robinhood_lookup_seen():
    broker = _rh(transport=lambda r, t: (200, {"found": True}))
    assert broker.lookup("k") is OrderLookup.SEEN


def test_robinhood_lookup_not_seen_404():
    broker = _rh(transport=lambda r, t: (404, {}))
    assert broker.lookup("k") is OrderLookup.NOT_SEEN


def test_robinhood_lookup_not_seen_found_false():
    broker = _rh(transport=lambda r, t: (200, {"found": False}))
    assert broker.lookup("k") is OrderLookup.NOT_SEEN


def test_robinhood_lookup_cannot_say_without_template():
    broker = RobinhoodAgenticBroker(lookup_template="")
    assert broker.lookup("k") is OrderLookup.CANNOT_SAY


def test_robinhood_lookup_cannot_say_on_transport_error():
    def down(request, timeout):
        raise TransportError("down")

    broker = _rh(transport=down)
    assert broker.lookup("k") is OrderLookup.CANNOT_SAY


def test_robinhood_lookup_cannot_say_on_ambiguous_body():
    broker = _rh(transport=lambda r, t: (200, {"status": "unknown"}))
    assert broker.lookup("k") is OrderLookup.CANNOT_SAY


# ── CLI verify/recover contract ──────────────────────────────────────────────
def test_verify_flags_an_intent_only_log_as_a_divergence(tmp_path):
    assert cmd_verify(argparse.Namespace(fills=_intent_only_log(tmp_path))) == 1


def test_old_log_without_intents_verifies_clean(tmp_path):
    path = tmp_path / "fills.jsonl"
    log = FillLog(path=path)
    log.record(event="buy", contract_key=QQQ, qty=1, price=1.05,
               reason="entry", note="etf-options")
    log.record(event="ack", contract_key=QQQ, qty=1, price=1.05,
               reason="entry", note="paper-1")
    log.record(event="latency", contract_key=QQQ, qty=0, price=0.0,
               reason="decision:entry")
    assert Ledger.unresolved(FillLog().replay_file(path)) == []
    assert cmd_verify(argparse.Namespace(fills=path)) == 0


class StubLookup:
    """A broker that only answers lookups; placing is forbidden in recover."""

    def __init__(self, results: dict[str, OrderLookup]):
        self.results = results
        self.lookups: list[str] = []
        self.placed = 0

    def lookup(self, idempotency_key: str) -> OrderLookup:
        self.lookups.append(idempotency_key)
        return self.results.get(idempotency_key, OrderLookup.CANNOT_SAY)

    def place(self, intent) -> None:
        self.placed += 1
        raise AssertionError("recover must never place an order")


def test_recover_seen_and_not_seen_append_terminals_and_verify_cleans(tmp_path, monkeypatch):
    path = _two_intent_log(tmp_path)
    stub = StubLookup({"key-seen": OrderLookup.SEEN, "key-not-sent": OrderLookup.NOT_SEEN})
    monkeypatch.setattr(cli, "make_recover_broker", lambda kind: stub)

    assert cmd_recover(argparse.Namespace(fills=path, broker="robinhood")) == 0

    rows = FillLog().replay_file(path)
    assert [f.event for f in rows] == ["intent", "intent", "recovered", "not_sent"]
    assert [f.note for f in rows[2:]] == ["key-seen", "key-not-sent"]
    assert Ledger.unresolved(rows) == []
    assert cmd_verify(argparse.Namespace(fills=path)) == 0
    assert stub.placed == 0


def test_recover_cannot_say_stays_unresolved_and_exits_1(tmp_path, monkeypatch):
    path = _intent_only_log(tmp_path)
    stub = StubLookup({KEY: OrderLookup.CANNOT_SAY})
    monkeypatch.setattr(cli, "make_recover_broker", lambda kind: stub)

    assert cmd_recover(argparse.Namespace(fills=path, broker="robinhood")) == 1

    rows = FillLog().replay_file(path)
    assert [f.event for f in rows] == ["intent"]          # nothing appended
    assert Ledger.unresolved(rows) == rows
    assert cmd_verify(argparse.Namespace(fills=path)) == 1
    assert stub.placed == 0


# ── replay parity ────────────────────────────────────────────────────────────
def test_replay_parity_is_unaffected_by_intent_rows():
    report = replay_file(TAPE)
    assert report.buys == 2 and report.sells == 2
    assert report.contracts_bought == 6
    assert report.realized_usd == pytest.approx(211.00, abs=0.01)
    # decisions name only buy/sell/skip/reject; the intent bookkeeping never leaks in
    assert all(not d.startswith(INTENT) for d in report.decisions)

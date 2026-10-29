"""Flatten: the operator's exit, and the three refusals that make it safe.

Every other exit in this stack is event-shaped - a Discord post, a breakeven quote, the
15:45 cutoff. This command is for the moment no post is coming (`killswitch` stops buys
but must never trap a position). It sells **confirmed** lots only, at a **bid it can
see**, and **never twice**: a lot with an unanswered sell is reported and left alone.

The dry run touches nothing; `--live` goes through the live listener, so the intent, the
venue answer, the confirmed book, the in-flight guard and the realised-P&L correction are
the same code an exit post uses.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from scorpion_mff import cli, flatten
from scorpion_mff.broker import Ack, PaperBroker
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.execution_book import ExecutionBook
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.types import OrderIntent, Quote

T0 = 1_757_340_000.0
QQQ = "QQQ|CALL|720|0DTE"
NVDA = "NVDA|PUT|227.5|2026-09-11"


class FixedQuotes:
    def __init__(self, bids: dict[str, float]) -> None:
        self.bids = bids

    def quote(self, contract_key: str) -> Quote | None:
        bid = self.bids.get(contract_key)
        return None if bid is None else Quote(bid=bid, ask=bid + 0.02, ts=T0)


class RejectingBroker:
    def __init__(self) -> None:
        self.placed: list[OrderIntent] = []

    def place(self, intent: OrderIntent) -> Ack:
        self.placed.append(intent)
        return Ack(ok=False, contract_key=intent.contract_key, qty=0,
                   price=intent.limit, error="position_not_found")


def buy(book: ExecutionBook, qty: int = 2, price: float = 1.0, key: str = QQQ,
        ts: float = T0) -> None:
    order = book.record_intent(
        OrderIntent(action="buy", qty=qty, limit=price, contract_key=key,
                    reason="entry:test", idempotency_key=f"{key}:buy:{ts}"), ts=ts)
    book.apply_ack(order.client_id,
                   Ack(ok=True, contract_key=key, qty=qty, price=price), ts=ts)


def listener_for(book: ExecutionBook, quotes: FixedQuotes,
                 broker: object | None = None) -> Listener:
    return Listener(
        transport=TapeTransport([]),
        engine=Engine(config=EngineConfig(pipe_test=False), fills=FillLog(),
                      clock=lambda: T0),
        broker=broker or PaperBroker(),
        quotes=quotes,
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=Path("live/heartbeat.json"), clock=lambda: T0),
        kill=KillSwitch(path="__never__", clock=lambda: T0),
        clock=lambda: T0,
        execution=book,
    )


# ── plan: no side effects, and the two things it refuses to invent ──────────
def test_a_plan_prices_every_lot_and_sends_nothing():
    book = ExecutionBook()
    buy(book, qty=2, price=1.0)
    report = flatten.plan(book, FixedQuotes({QQQ: 1.20}))

    assert report.exit_code == 0
    line = report.lines[0]
    assert line.status == flatten.PLANNED
    assert line.bid == 1.20
    assert line.estimated_pnl_usd == pytest.approx(40.0)
    assert book.open_positions()[0].contracts == 2       # untouched
    assert "dry run" in report.to_text()


def test_a_plan_leaves_an_unpriced_lot_on_the_book_and_exits_one():
    book = ExecutionBook()
    buy(book, qty=2, price=1.0)
    report = flatten.plan(book, FixedQuotes({}))

    assert report.exit_code == 1
    assert report.lines[0].status == flatten.UNPRICED
    assert report.lines[0].bid is None


def test_a_plan_does_not_plan_a_second_sell_for_a_lot_already_in_flight():
    book = ExecutionBook()
    buy(book, qty=2, price=1.0)
    book.record_intent(
        OrderIntent(action="sell", qty=2, limit=1.20, contract_key=QQQ,
                    reason="close", idempotency_key=f"{QQQ}:close:1"), ts=T0)

    report = flatten.plan(book, FixedQuotes({QQQ: 1.20}))
    assert report.lines[0].status == flatten.IN_FLIGHT
    assert report.exit_code == 0               # already being exited is not a failure


# ── execute: the live ladder, and the book is the list ──────────────────────
def test_execute_sells_every_confirmed_lot_at_the_bid_and_ends_flat():
    book = ExecutionBook()
    buy(book, qty=2, price=1.0)
    quotes = FixedQuotes({QQQ: 1.20})
    listener = listener_for(book, quotes)

    report = flatten.execute(book, listener.engine, listener, quotes, T0)
    assert report.exit_code == 0
    assert report.flat
    assert report.lines[0].status == flatten.PLACED
    assert book.open_positions() == []
    # The engine's realised P&L is the confirmed fill: (1.20 - 1.00) * 2 * 100.
    assert listener.engine.stats.realized_usd == pytest.approx(40.0)
    assert listener.engine.open_positions == []


def test_execute_twice_is_a_no_op_once_the_book_is_flat():
    book = ExecutionBook()
    buy(book, qty=1, price=1.0)
    quotes = FixedQuotes({QQQ: 1.20})
    listener = listener_for(book, quotes)
    flatten.execute(book, listener.engine, listener, quotes, T0)

    second = flatten.execute(book, listener.engine, listener, quotes, T0 + 60)
    assert second.exit_code == 0
    assert second.lines == []
    assert "nothing to flatten" in second.to_text()


def test_a_rejected_sell_is_named_the_book_keeps_the_lot_and_the_engine_restores_it():
    book = ExecutionBook()
    buy(book, qty=2, price=1.0)
    quotes = FixedQuotes({QQQ: 1.20})
    listener = listener_for(book, quotes, broker=RejectingBroker())

    report = flatten.execute(book, listener.engine, listener, quotes, T0)
    assert report.exit_code == 1
    assert report.lines[0].status == flatten.FAILED
    assert "exit_failed" in report.page
    assert book.open_positions()[0].contracts == 2
    assert listener.engine.positions[QQQ].contracts == 2
    # The refused sell never happened, so the assumed profit is not left booked.
    assert listener.engine.stats.realized_usd == pytest.approx(0.0)


def test_execute_sells_the_priced_lot_and_names_the_unpriced_one():
    book = ExecutionBook()
    buy(book, qty=1, price=1.0, key=QQQ)
    buy(book, qty=1, price=2.0, key=NVDA)
    quotes = FixedQuotes({QQQ: 1.20})
    listener = listener_for(book, quotes)

    report = flatten.execute(book, listener.engine, listener, quotes, T0)
    statuses = {line.contract_key: line.status for line in report.lines}
    assert statuses[QQQ] == flatten.PLACED
    assert statuses[NVDA] == flatten.UNPRICED
    assert report.exit_code == 1
    assert [p.contract_key for p in book.open_positions()] == [NVDA]
    assert "unpriced" in report.page


def test_an_expired_lot_is_released_not_sold():
    book = ExecutionBook()
    buy(book, qty=1, price=1.0, ts=T0 - 86_400.0)          # opened the previous session
    quotes = FixedQuotes({QQQ: 0.01})
    listener = listener_for(book, quotes)

    report = flatten.execute(book, listener.engine, listener, quotes, T0)
    assert report.expired == 1
    assert report.flat
    assert report.exit_code == 0
    assert all(line.status != flatten.PLACED for line in report.lines)


# ── the CLI: dry by default, and the refusals ───────────────────────────────
def test_cli_dry_run_prints_a_plan_and_places_nothing(tmp_path, capsys, monkeypatch):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    buy(book, qty=2, price=1.0)
    monkeypatch.setattr(cli, "EXECUTION_LOG", path)

    code = cli.cmd_flatten(Namespace(live=False, json=False),
                           quotes=FixedQuotes({QQQ: 1.20}))
    out = capsys.readouterr().out
    assert code == 0
    assert "flatten (PLAN)" in out and "dry run" in out
    assert "QQQ|CALL|720|0DTE" in out
    assert ExecutionBook.load(path).open_positions()[0].contracts == 2


def test_cli_json_plan_is_machine_readable(tmp_path, capsys, monkeypatch):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    buy(book, qty=1, price=1.0)
    monkeypatch.setattr(cli, "EXECUTION_LOG", path)

    code = cli.cmd_flatten(Namespace(live=False, json=True),
                           quotes=FixedQuotes({QQQ: 1.20}))
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "plan" and payload["lines"][0]["status"] == "planned"


def test_cli_live_needs_a_token_and_otherwise_does_nothing(tmp_path, capsys, monkeypatch):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    buy(book, qty=1, price=1.0)
    monkeypatch.setattr(cli, "EXECUTION_LOG", path)
    monkeypatch.delenv("MFF_RH_TOKEN", raising=False)

    code = cli.cmd_flatten(Namespace(live=True, json=False),
                           quotes=FixedQuotes({QQQ: 1.20}))
    assert code == 1
    assert "MFF_RH_TOKEN" in capsys.readouterr().err
    assert ExecutionBook.load(path).open_positions()[0].contracts == 1


def test_cli_live_places_through_the_listener(tmp_path, capsys, monkeypatch):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    buy(book, qty=2, price=1.0)
    monkeypatch.setattr(cli, "EXECUTION_LOG", path)
    monkeypatch.setattr(cli, "FILL_LOG", tmp_path / "fills.jsonl")
    monkeypatch.setattr(cli, "HEARTBEAT", tmp_path / "heartbeat.json")
    monkeypatch.setattr(cli, "NO_TRADE", tmp_path / "NO_TRADE")
    monkeypatch.setenv("MFF_RH_TOKEN", "test-token")
    monkeypatch.setattr(cli, "RobinhoodAgenticBroker", lambda dry_run: PaperBroker())

    code = cli.cmd_flatten(Namespace(live=True, json=False),
                           quotes=FixedQuotes({QQQ: 1.20}))
    out = capsys.readouterr().out
    assert code == 0
    assert "flatten (LIVE)" in out and "0 contract(s) still open" in out
    assert ExecutionBook.load(path).open_positions() == []


def test_cli_refuses_a_corrupt_execution_log(tmp_path, capsys, monkeypatch):
    path = tmp_path / "execution.jsonl"
    path.write_text('garbage\n{"v": 1}\n', encoding="utf-8")
    monkeypatch.setattr(cli, "EXECUTION_LOG", path)

    code = cli.cmd_flatten(Namespace(live=False, json=False),
                           quotes=FixedQuotes({QQQ: 1.20}))
    assert code == 2
    assert "cannot be trusted" in capsys.readouterr().err

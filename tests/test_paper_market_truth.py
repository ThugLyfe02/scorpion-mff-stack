"""The paper market never manufactures a fill from a price-only observation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scorpion_mff.broker import BrokerError
from scorpion_mff.paper_capture import run_capture
from scorpion_mff.paper_market import MarketQuote, PaperQuoteBook, QuoteDrivenPaperBroker
from scorpion_mff.types import OrderIntent

T = datetime(2026, 11, 23, 15, 0, tzinfo=UTC).timestamp()
OSI = "QQQ   261123C00719000"
KEY = "QQQ|CALL|719|0DTE"


def quote(ts: float = T, bid: float = 1.01, ask: float = 1.05,
          size: int = 2) -> MarketQuote:
    return MarketQuote(OSI, ts, T, bid, ask, size, size)


def order(key: str, side: str = "buy", qty: int = 1, limit: float = 1.05) -> OrderIntent:
    return OrderIntent(side, qty, limit, KEY, "fixture", key)


def test_marketability_and_displayed_size() -> None:
    book = PaperQuoteBook(lambda: T)
    assert book.admit(quote()) == "accepted"
    broker = QuoteDrivenPaperBroker(book)
    with pytest.raises(BrokerError, match="nonmarketable"):
        broker.place(order("resting", limit=1.04))
    with pytest.raises(BrokerError, match="insufficient_ask_size"):
        broker.place(order("oversize", qty=3))
    first = broker.place(order("a"))
    assert first.qty == 1 and first.price == 1.05
    assert broker.place(order("a")) == first
    with pytest.raises(BrokerError, match="conflicting_idempotency"):
        broker.place(order("a", qty=2))
    broker.place(order("b"))
    with pytest.raises(BrokerError, match="insufficient_ask_size"):
        broker.place(order("c"))
    assert book.admit(quote()) == "duplicate_or_reordered_quote"
    with pytest.raises(BrokerError, match="insufficient_ask_size"):
        broker.place(order("d"))


def test_sell_requires_inventory_and_marketability() -> None:
    book = PaperQuoteBook(lambda: T)
    book.admit(quote())
    broker = QuoteDrivenPaperBroker(book)
    with pytest.raises(BrokerError, match="no_confirmed_inventory"):
        broker.place(order("short", "sell", limit=1.01))
    broker.place(order("buy"))
    with pytest.raises(BrokerError, match="nonmarketable_sell"):
        broker.place(order("wrong_price", "sell", limit=1.02))
    assert broker.place(order("close", "sell", limit=1.01)).qty == 1
    assert broker.inventory[OSI] == 0


def test_gap_and_delayed_events_do_not_restore_price() -> None:
    book = PaperQuoteBook(lambda: T)
    assert book.admit(quote(T - 4)) == "stale_quote"
    assert book.admit(quote(T + 1)) == "noncausal_quote"
    assert book.admit(quote()) == "accepted"
    book.gap()
    assert book.quote(KEY) is None
    assert book.admit(quote()) == "duplicate_or_reordered_quote"


def test_regular_hours_and_freshness() -> None:
    clock = [T]
    book = PaperQuoteBook(lambda: clock[0])
    book.admit(quote())
    assert book.quote(KEY) is not None
    clock[0] += 3.001
    assert book.quote(KEY) is None
    clock[0] = datetime(2026, 11, 23, 13, 0, tzinfo=UTC).timestamp()
    assert book.quote(KEY) is None
    with pytest.raises(BrokerError, match="outside_regular_session"):
        QuoteDrivenPaperBroker(book).place(order("early"))


def test_wrong_expiry_and_invalid_quote_are_refused() -> None:
    clock = [T]
    book = PaperQuoteBook(lambda: clock[0])
    book.admit(quote())
    assert book.quote("QQQ|CALL|719|2026-11-24") is None
    clock[0] += 1
    invalid = MarketQuote(OSI, clock[0], clock[0], 2.0, 1.0, 2, 2)
    assert book.admit(invalid) == "invalid_or_withdrawn_market"
    assert book.quote(KEY) is None


def capture() -> list[str]:
    rows = [
        dict(kind="quote", received_ts=T, event_ts=T, osi=OSI,
             bid=1.01, ask=1.05, bid_size=2, ask_size=2),
        dict(kind="signal", received_ts=T, source_ts=T, channel="etf-options",
             author_id="authorized", message_id="fixture-1",
             text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01"),
        dict(kind="quote", received_ts=T+1, event_ts=T+1, osi=OSI,
             bid=1.20, ask=1.25, bid_size=2, ask_size=2),
        dict(kind="signal", received_ts=T+1, source_ts=T+1, channel="etf-options",
             author_id="authorized", message_id="fixture-2",
             text="QQQ CALLs closed at 1.20"),
    ]
    return [json.dumps(dict(row, seq=i))+"\n" for i, row in enumerate(rows, 1)]


def test_capture_runs_existing_engine_and_replays_durable_modeled_book(tmp_path: Path) -> None:
    kwargs = dict(source="synthetic fixture", allowed_authors=frozenset({"authorized"}))
    summary = run_capture(capture(), tmp_path / "first", **kwargs)
    assert summary["modeled_fills"] == 2
    assert summary["modeled_realized_usd"] == pytest.approx(15.0)
    assert summary["ledger_integrity"] and summary["session_flat"]
    assert not summary["certifies_live_fills"]
    second = run_capture(capture(), tmp_path / "second", **kwargs)
    assert summary == second
    with pytest.raises(FileExistsError):
        run_capture(capture(), tmp_path / "first", **kwargs)


def test_capture_sequence_gap_leaves_no_completion_receipt(tmp_path: Path) -> None:
    rows = capture()
    rows.pop(1)
    out = tmp_path / "gap"
    with pytest.raises(ValueError, match="sequence"):
        run_capture(rows, out, source="fixture", allowed_authors=frozenset({"authorized"}))
    assert not (out / "summary.json").exists()


def test_wrong_author_is_not_an_execution_signal(tmp_path: Path) -> None:
    report = run_capture(capture(), tmp_path / "refused", source="fixture",
                         allowed_authors=frozenset({"other"}))
    assert report["modeled_fills"] == 0
    assert report["admission_counts"]["unauthorized_signal"] == 2


def test_explicit_wrong_strike_cannot_close_another_contract() -> None:
    from scorpion_mff.engine import Engine
    from scorpion_mff.types import AlertKind, Position, Side, Signal

    engine = Engine(clock=lambda: T)
    engine.positions[KEY] = Position(KEY, "QQQ", Side.CALL, 1, 1.05, T)
    signal = Signal(kind=AlertKind.CLOSE, ticker="QQQ", side=Side.CALL,
                    strike=720, expiry="0DTE", price=1.20)
    assert engine._resolve(signal) is None
    assert engine.positions[KEY].contracts == 1


def test_explicit_wrong_expiry_cannot_close_another_contract() -> None:
    from scorpion_mff.engine import Engine
    from scorpion_mff.types import AlertKind, Position, Side, Signal

    engine = Engine(clock=lambda: T)
    engine.positions[KEY] = Position(KEY, "QQQ", Side.CALL, 1, 1.05, T)
    signal = Signal(kind=AlertKind.CLOSE, ticker="QQQ", side=Side.CALL,
                    strike=719, expiry="2026-11-24", price=1.20)
    assert engine._resolve(signal) is None


def test_partial_exit_identity_emits_the_resolved_held_key() -> None:
    from scorpion_mff.engine import Engine
    from scorpion_mff.types import AlertKind, Position, Side, Signal

    engine = Engine(clock=lambda: T)
    engine.day = engine.trading_day(T)
    engine.positions[KEY] = Position(KEY, "QQQ", Side.CALL, 1, 1.05, T)
    signal = Signal(kind=AlertKind.CLOSE, ticker="QQQ", side=Side.CALL,
                    strike=719, price=1.20)
    intents = engine.on_signal(signal)
    assert len(intents) == 1
    assert intents[0].contract_key == KEY

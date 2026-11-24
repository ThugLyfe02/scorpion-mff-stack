"""The paper market never manufactures a fill from a price-only observation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scorpion_mff.account import FeeModel
from scorpion_mff.broker import BrokerError
from scorpion_mff.execution_book import ExecutionBook
from scorpion_mff.paper_capture import compare_capture_venue, run_capture
from scorpion_mff.paper_market import MarketQuote, PaperQuoteBook, QuoteDrivenPaperBroker
from scorpion_mff.reconcile import ReconcileError
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


def test_same_timestamp_conflict_withdraws_liquidity() -> None:
    book = PaperQuoteBook(lambda: T)
    assert book.admit(quote()) == "accepted"
    assert book.admit(quote(bid=0)) == "conflicting_quote_timestamp"
    assert book.quote(KEY) is None
    assert book.admit(quote()) == "duplicate_or_reordered_quote"
    assert book.quote(KEY) is None


def test_halt_reopen_and_disconnect_require_new_status_and_quote() -> None:
    clock = [T]
    book = PaperQuoteBook(lambda: clock[0], require_market_status=True)
    assert book.admit(quote()) == "market_status_missing"
    assert book.admit_status(OSI, T, "TRADING") == "accepted"
    clock[0] += 1
    row = MarketQuote(OSI, clock[0], clock[0], 1.01, 1.05, 2, 2)
    assert book.admit(row) == "accepted"
    assert book.quote(KEY) is not None
    assert book.admit_status(OSI, clock[0], "HALTED") == "accepted"
    assert book.quote(KEY) is None
    assert book.admit_status(OSI, clock[0], "TRADING") == "conflicting_status_timestamp"
    assert book.quote(KEY) is None
    clock[0] += 1
    assert book.admit_status(OSI, clock[0], "TRADING") == "accepted"
    assert book.quote(KEY) is None
    clock[0] += 1
    row = MarketQuote(OSI, clock[0], clock[0], 1.01, 1.05, 2, 2)
    assert book.admit(row) == "accepted"
    book.gap()
    assert book.admit_status(OSI, clock[0]-1, "TRADING") == "duplicate_or_reordered_status"
    assert book.quote(KEY) is None


def strict_capture() -> list[str]:
    rows = [json.loads(line) for line in capture()]
    rows.insert(0, dict(kind="market_status", received_ts=T-1, event_ts=T-1,
                        osi=OSI, status="TRADING"))
    return [json.dumps(dict(row, seq=i))+"\n" for i, row in enumerate(rows, 1)]


def test_strict_sample_reconciles_cashflows_and_declared_fees(tmp_path: Path) -> None:
    report = run_capture(
        strict_capture(), tmp_path / "strict", source="fixture",
        allowed_authors=frozenset({"authorized"}), require_market_status=True,
        fee_model=FeeModel(per_contract_venue_cents=65), max_signal_age_s=10,
    )
    assert report["simulator_fill_match"] and report["simulator_inventory_match"]
    assert report["flat_cashflow_match"] and report["modeled_sample_complete"]
    assert report["modeled_cashflow_usd"] == 15.0
    assert report["modeled_fees_usd"] == 1.30
    assert report["modeled_net_realized_usd"] == 13.70
    assert not report["certifies_live_fills"]


def test_flat_without_fills_is_not_a_qualified_sample(tmp_path: Path) -> None:
    report = run_capture(
        capture(), tmp_path / "missing-status", source="fixture",
        allowed_authors=frozenset({"authorized"}), require_market_status=True,
        fee_model=FeeModel(), max_signal_age_s=10,
    )
    assert report["session_flat"] and report["ledger_integrity"]
    assert report["modeled_fills"] == 0
    assert not report["modeled_sample_complete"]
    assert report["modeled_net_realized_usd"] is None
    assert "no_modeled_fills" in report["sample_blockers"]


def test_stale_signal_cannot_create_modeled_entry(tmp_path: Path) -> None:
    rows = [json.loads(line) for line in strict_capture()]
    rows[2]["source_ts"] -= 20
    report = run_capture(
        [json.dumps(row)+"\n" for row in rows], tmp_path / "stale", source="fixture",
        allowed_authors=frozenset({"authorized"}), require_market_status=True,
        fee_model=FeeModel(), max_signal_age_s=10,
    )
    assert report["admission_counts"]["stale_signal"] == 1
    assert report["modeled_fills"] == 0
    assert not report["modeled_sample_complete"]


def test_legacy_capture_does_not_invent_net_return(tmp_path: Path) -> None:
    report = run_capture(capture(), tmp_path / "legacy", source="fixture",
                         allowed_authors=frozenset({"authorized"}))
    assert report["modeled_realized_usd"] == 15.0
    assert report["modeled_net_realized_usd"] is None
    assert not report["modeled_sample_complete"]


def venue_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    run = tmp_path / "reconcile-run"
    summary = run_capture(
        strict_capture(), run, source="synthetic model source",
        allowed_authors=frozenset({"authorized"}), require_market_status=True,
        fee_model=FeeModel(), max_signal_age_s=10,
    )
    book = ExecutionBook.load(run / "modeled_execution.jsonl")
    payload = {
        "schema": "scorpion.paper-venue-export.v1", "authority": "PAPER_ONLY",
        "source": "independent fixture", "input_sha256": summary["input_sha256"],
        "session_date": summary["session_date"], "as_of_ts": T+2,
        "complete_order_export": True, "positions": [],
        "orders": [
            dict(client_id=o.client_id, osi=OSI, action=o.action,
                 requested_qty=o.intent.qty, limit=str(o.intent.limit),
                 filled_qty=o.filled_qty, average_price=str(o.avg_price), state=o.state)
            for o in book.orders.values()
        ],
    }
    export = tmp_path / "venue.json"
    export.write_text(json.dumps(payload))
    return run, export, payload


def test_paper_venue_export_exact_match_is_not_live_certification(tmp_path: Path) -> None:
    run, export, _ = venue_fixture(tmp_path)
    report = compare_capture_venue(run, export)
    assert report["exact_parity"]
    assert report["orders"]["equal_quantity_price_samples"] == 2
    assert report["orders"]["external_minus_modeled_cost_usd"] == "0.00"
    assert report["authority"] == "RESEARCH_ONLY"
    assert not report["certifies_live_fills"]


def test_venue_missing_orders_are_not_zero_fills(tmp_path: Path) -> None:
    run, export, payload = venue_fixture(tmp_path)
    payload["orders"].pop()
    export.write_text(json.dumps(payload))
    report = compare_capture_venue(run, export)
    assert not report["exact_parity"]
    assert len(report["orders"]["missing_order_ids"]) == 1
    assert not report["orders"]["coverage_complete"]


def test_venue_price_differences_are_signed_and_positions_reconciled(tmp_path: Path) -> None:
    run, export, payload = venue_fixture(tmp_path)
    buy = next(o for o in payload["orders"] if o["action"] == "buy")
    buy["average_price"] = "1.04"
    payload["positions"] = [dict(contract_key=OSI, contracts=1)]
    export.write_text(json.dumps(payload))
    report = compare_capture_venue(run, export)
    assert report["orders"]["external_minus_modeled_cost_usd"] == "-1.00"
    assert not report["positions"]["clean"]
    assert not report["exact_parity"]


def test_venue_scope_and_local_ledger_tamper_refuse(tmp_path: Path) -> None:
    run, export, payload = venue_fixture(tmp_path)
    payload["authority"] = "LIVE"
    export.write_text(json.dumps(payload))
    with pytest.raises(ReconcileError, match="scope"):
        compare_capture_venue(run, export)
    payload["authority"] = "PAPER_ONLY"
    export.write_text(json.dumps(payload))
    ledger = run / "modeled_execution.jsonl"
    ledger.write_bytes(ledger.read_bytes() + b"\n")
    with pytest.raises(ReconcileError, match="unverified"):
        compare_capture_venue(run, export)

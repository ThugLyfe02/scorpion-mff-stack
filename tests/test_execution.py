"""The execution book: confirmed fills, restarts, rejects and partials."""

from __future__ import annotations

import json

import pytest

from scorpion_mff.broker import Ack
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.execution import (
    STATE_CANCELLED,
    STATE_FILLED,
    STATE_PARTIAL,
    STATE_REJECTED,
    ExecutionBook,
    ExecutionError,
    reconcile,
)
from scorpion_mff.types import OrderIntent

KEY = "QQQ|CALL|719|0DTE"
T0 = 1_772_000_000.0


def intent(action: str = "buy", qty: int = 10, limit: float = 1.0,
           key: str = KEY, reason: str = "entry:standard",
           idem: str = "test-1") -> OrderIntent:
    return OrderIntent(action=action, qty=qty, limit=limit, contract_key=key,
                       reason=reason, idempotency_key=idem)


def ack(qty: int = 10, price: float = 1.0, ok: bool = True,
        key: str = KEY, error: str = "") -> Ack:
    return Ack(ok=ok, contract_key=key, qty=qty, price=price,
               broker_order_id="broker-1", error=error)


# ── fills ────────────────────────────────────────────────────────────────────
def test_a_full_buy_then_sell_realises_the_difference():
    book = ExecutionBook()
    buy = book.record_intent(intent(), ts=T0, channel="etf-options")
    book.apply_ack(buy.client_id, ack(10, 1.0), ts=T0)
    assert [(p.contract_key, p.contracts) for p in book.open_positions()] == [(KEY, 10)]

    sell = book.record_intent(intent("sell", 10, 1.5, reason="close:pct=50%", idem="test-2"),
                              ts=T0 + 60, channel="etf-options")
    book.apply_ack(sell.client_id, ack(10, 1.5), ts=T0 + 60)
    assert book.open_positions() == []
    assert book.realized_total == pytest.approx(500.0)
    assert book.closed_keys("2026-02-25") == {KEY}
    assert sell.state == STATE_FILLED


def test_a_partial_ack_creates_only_the_filled_quantity():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=10), ts=T0)
    book.apply_ack(order.client_id, ack(4, 1.0), ts=T0)
    assert order.state == STATE_PARTIAL
    assert book.open_positions()[0].contracts == 4

    # A second cumulative ack of 10 applies the delta at the new price.
    book.apply_ack(order.client_id, ack(10, 1.2), ts=T0 + 30)
    lot = book.open_positions()[0]
    assert order.state == STATE_FILLED
    assert lot.contracts == 10
    assert lot.avg_price == pytest.approx((4 * 1.0 + 6 * 1.2) / 10)


def test_a_rejected_buy_leaves_no_position():
    book = ExecutionBook()
    order = book.record_intent(intent(), ts=T0)
    book.apply_ack(order.client_id, ack(0, 0.0, ok=False, error="insufficient buying power"),
                   ts=T0)
    assert order.state == STATE_REJECTED
    assert book.open_positions() == []
    assert "insufficient" in order.reason


def test_a_rejected_sell_keeps_the_lot_and_the_first_fill():
    book = ExecutionBook()
    buy = book.record_intent(intent(), ts=T0)
    book.apply_ack(buy.client_id, ack(10, 1.0), ts=T0)
    sell = book.record_intent(intent("sell", 10, 1.5, reason="close", idem="sell-1"),
                              ts=T0 + 60)
    book.apply_ack(sell.client_id, ack(4, 1.5), ts=T0 + 60)   # partial fill
    book.reject(sell.client_id, reason="venue timeout", ts=T0 + 70)

    assert sell.state == STATE_CANCELLED
    assert book.open_positions()[0].contracts == 6
    assert book.realized_total == pytest.approx(200.0)


def test_an_oversell_is_clamped_and_counted():
    book = ExecutionBook()
    buy = book.record_intent(intent(qty=5), ts=T0)
    book.apply_ack(buy.client_id, ack(5, 1.0), ts=T0)
    sell = book.record_intent(intent("sell", 10, 2.0, reason="close", idem="sell-2"),
                              ts=T0 + 60)
    book.apply_ack(sell.client_id, ack(10, 2.0), ts=T0 + 60)

    assert book.open_positions() == []
    assert book.realized_total == pytest.approx(500.0)   # 5 contracts, not 9
    assert book.anomalies["oversell_clamped"] == 1


def test_an_ack_cannot_regress():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=10), ts=T0)
    book.apply_ack(order.client_id, ack(6, 1.0), ts=T0)
    with pytest.raises(ExecutionError, match="regressed"):
        book.apply_ack(order.client_id, ack(5, 1.0), ts=T0 + 10)


def test_a_late_incremental_fill_is_clamped_to_the_order():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=5), ts=T0)
    book.apply_ack(order.client_id, ack(2, 1.0), ts=T0)
    book.record_fill(order.client_id, qty=99, price=1.1, ts=T0 + 30)
    assert order.filled_qty == 5
    assert book.open_positions()[0].contracts == 5


# ── persistence and restart ──────────────────────────────────────────────────
def test_the_log_round_trips_and_restores_the_book(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    buy = book.record_intent(intent(), ts=T0, channel="etf-options")
    book.apply_ack(buy.client_id, ack(10, 1.0), ts=T0)

    restored = ExecutionBook.load(path)
    assert restored.snapshot()["positions"] == book.snapshot()["positions"]
    assert restored.realized_total == book.realized_total
    assert restored.orders[buy.client_id].state == STATE_FILLED


def test_client_ids_do_not_collide_across_days(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    first = book.record_intent(intent(), ts=T0, channel="etf-options")
    second = book.record_intent(intent(), ts=T0 + 86_400, channel="etf-options")
    assert first.client_id != second.client_id
    assert len(book.orders) == 2


def test_a_torn_last_line_is_ignored_but_middle_corruption_refuses(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    order = book.record_intent(intent(), ts=T0)
    book.apply_ack(order.client_id, ack(10, 1.0), ts=T0)

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"t":"ack","cid":')       # crash mid-append
    restored = ExecutionBook.load(path)
    assert restored.open_positions()[0].contracts == 10

    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(0, "{broken")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ExecutionError, match="corrupt"):
        ExecutionBook.load(path)


# ── engine synchronisation ──────────────────────────────────────────────────
def _engine() -> Engine:
    return Engine(config=EngineConfig(pipe_test=False), clock=lambda: T0)


def test_sync_restores_positions_realised_and_closed_keys():
    book = ExecutionBook()
    buy = book.record_intent(intent(), ts=T0, channel="etf-options")
    book.apply_ack(buy.client_id, ack(10, 1.0), ts=T0)
    sell = book.record_intent(intent("sell", 4, 1.5, reason="trim:1/1", idem="sell-3"),
                              ts=T0 + 60)
    book.apply_ack(sell.client_id, ack(4, 1.5), ts=T0 + 60)

    engine = _engine()
    book.sync_engine(engine, T0 + 60)

    assert engine.positions[KEY].contracts == 6
    assert engine.positions[KEY].trimmed == 1
    assert engine.stats.realized_usd == pytest.approx(200.0)
    assert engine.governor_state.realized_today_usd == pytest.approx(200.0)
    assert engine.stats.trades_today == 1


def test_a_restart_rebuilds_the_engine_from_disk(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    buy = book.record_intent(intent(), ts=T0, channel="etf-options")
    book.apply_ack(buy.client_id, ack(10, 1.0), ts=T0)

    restarted = ExecutionBook.load(path)
    engine = _engine()
    restarted.sync_engine(engine, T0 + 10)
    assert engine.positions[KEY].contracts == 10
    assert engine.stats.realized_usd == 0.0


def test_baseline_preserves_prior_equity_drawdown_and_day_counters():
    book = ExecutionBook()
    book.ensure_baseline(equity_usd=1000.0, peak_equity_usd=1500.0, day="2026-02-25",
                         realized_today_usd=-100.0, trades_today=2,
                         consecutive_losses=1)
    buy = book.record_intent(intent(), ts=T0, channel="etf-options")
    book.apply_ack(buy.client_id, ack(10, 1.0), ts=T0)
    sell = book.record_intent(intent("sell", 10, 0.9, reason="close", idem="sell-4"),
                              ts=T0 + 60)
    book.apply_ack(sell.client_id, ack(10, 0.9), ts=T0 + 60)

    engine = _engine()
    book.sync_engine(engine, T0 + 60)
    assert engine.stats.realized_usd == pytest.approx(900.0)  # 1000 - 100
    assert engine.governor_state.peak_equity_usd == 1500.0
    assert engine.governor_state.realized_today_usd == pytest.approx(-200.0)
    assert engine.governor_state.consecutive_losses == 2
    assert engine.stats.trades_today == 3


def test_reconcile_reports_quantity_drift():
    book = ExecutionBook()
    buy = book.record_intent(intent(), ts=T0)
    book.apply_ack(buy.client_id, ack(10, 1.0), ts=T0)
    engine = _engine()
    book.sync_engine(engine, T0)
    report = reconcile(book, engine.open_positions)
    assert report["matched"] is True

    engine.positions[KEY].contracts = 7
    report = reconcile(book, engine.open_positions)
    assert report["matched"] is False
    assert report["differences"][0]["delta"] == -3


def test_report_is_serialisable_and_names_open_orders():
    book = ExecutionBook()
    order = book.record_intent(intent(), ts=T0)
    book.apply_ack(order.client_id, ack(4, 1.0), ts=T0)
    text = book.report_text()
    assert "execution book" in text
    assert "PARTIAL" in text
    json.dumps(book.snapshot())


def test_a_carried_0dte_lot_is_closed_at_zero_on_the_next_day():
    book = ExecutionBook()
    buy = book.record_intent(intent(), ts=T0, channel="etf-options")
    book.apply_ack(buy.client_id, ack(10, 1.0), ts=T0)
    engine = _engine()
    book.sync_engine(engine, T0 + 86_400)

    assert book.open_positions() == []
    assert book.realized_total == pytest.approx(-1000.0)   # premium, max loss
    assert book.anomalies["expired_at_zero"] == 1
    assert engine.positions == {}
    assert engine.stats.realized_usd == pytest.approx(-1000.0)


def test_a_same_day_0dte_lot_is_not_expired_by_a_sync():
    book = ExecutionBook()
    buy = book.record_intent(intent(), ts=T0, channel="etf-options")
    book.apply_ack(buy.client_id, ack(10, 1.0), ts=T0)
    engine = _engine()
    book.sync_engine(engine, T0 + 60)
    assert book.open_positions()[0].contracts == 10

"""The confirmed execution book: cumulative acks, partials, restart replay.

Every test here is offline and deterministic. The theme is one question asked from
several directions: **does the book state what the venue confirmed, and only that?**

The four defects this suite exists to keep out, all of them real before this module:

* a rejected buy staying in the book (holding a concurrency slot forever);
* a partial fill recorded as the full clip (overstating realised P&L);
* a rejected sell having already moved realised P&L and the drawdown;
* a restart forgetting every position (re-entering a contract it still holds, or
  failing to price an exit for one it does).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scorpion_mff import cli
from scorpion_mff.broker import Ack, BrokerError, PaperBroker, TransportError
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.execution_book import (
    CANCELLED,
    FILLED,
    PARTIAL,
    PENDING,
    REJECTED,
    ExecutionBook,
    ExecutionBookError,
    contract_parts,
    utc_day,
)
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.types import OrderIntent, Position, Quote, Side

T0 = 1_757_340_000.0                     # a fixed epoch: every test uses one clock
DAY = utc_day(T0)
QQQ = "QQQ|CALL|720|0DTE"
NVDA = "NVDA|PUT|227.5|2026-09-11"


# ── helpers ──────────────────────────────────────────────────────────────────
def intent(action: str = "buy", qty: int = 5, limit: float = 1.0,
           key: str = QQQ, reason: str = "entry:FULL:his_bid=1.00:ask=1.05",
           idem: str = "") -> OrderIntent:
    return OrderIntent(action=action, qty=qty, limit=limit, contract_key=key,
                       reason=reason,
                       idempotency_key=idem or f"{key}:{action}:{limit:g}:{qty}")


def ack(qty: int, price: float = 1.0, key: str = QQQ, ok: bool = True,
        error: str = "", broker_id: str = "rh-1") -> Ack:
    return Ack(ok=ok, contract_key=key, qty=qty, price=price,
               broker_order_id=broker_id, error=error)


def fill_buy(book: ExecutionBook, qty: int, price: float, ts: float = T0,
             key: str = QQQ) -> str:
    """Book a buy intent and confirm it in ONE ack; returns the client id."""
    order = book.record_intent(intent(qty=qty, limit=price, key=key), ts=ts)
    book.apply_ack(order.client_id, ack(qty, price, key=key), ts=ts)
    return order.client_id


# ── the split fill: 5 == 3 + 2 ────────────────────────────────────────────────
def test_a_five_lot_that_fills_three_then_two_is_one_five_lot():
    """The realised R of a split fill must equal the realised R of one fill.

    The venue reporting "3 filled, now 5 filled" is the same trade as "5 filled".
    Booking the second ack as another 5 (or as 2 against a fresh lot) is how a
    partial fill becomes double P&L - the exact failure a cumulative ack prevents.
    """
    split = ExecutionBook()
    order = split.record_intent(intent(qty=5, limit=1.00), ts=T0)
    split.apply_ack(order.client_id, ack(3, 1.00), ts=T0)
    split.apply_ack(order.client_id, ack(5, 1.00), ts=T0 + 1)
    split.record_intent(intent("sell", qty=5, limit=1.50), ts=T0 + 2)
    sell = split.orders[list(split.orders)[-1]]
    split.apply_ack(sell.client_id, ack(5, 1.50), ts=T0 + 2)

    once = ExecutionBook()
    single = once.record_intent(intent(qty=5, limit=1.00), ts=T0)
    once.apply_ack(single.client_id, ack(5, 1.00), ts=T0)
    once.record_intent(intent("sell", qty=5, limit=1.50), ts=T0 + 2)
    sell_once = once.orders[list(once.orders)[-1]]
    once.apply_ack(sell_once.client_id, ack(5, 1.50), ts=T0 + 2)

    assert split.realized_total == once.realized_total == 250.0    # (1.50-1.00)*5*100
    assert len(split.realized) == len(once.realized) == 1
    assert split.realized[0].qty == 5
    assert split.open_positions() == once.open_positions() == []
    assert split.closed_keys(DAY) == once.closed_keys(DAY) == {QQQ}
    # Three fill rows in the split (3, 2, sell 5) versus two in the single (5, sell 5);
    # the STATE is identical, which is the claim.
    assert [f.qty for f in split.fills] == [3, 2, 5]
    assert [f.cumulative for f in split.fills] == [3, 5, 5]
    assert [f.qty for f in once.fills] == [5, 5]


def test_a_second_ack_applies_only_the_delta_and_averages_the_price():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=5, limit=1.00), ts=T0)
    book.apply_ack(order.client_id, ack(2, 1.00), ts=T0)
    book.apply_ack(order.client_id, ack(3, 1.30), ts=T0 + 1)

    assert order.filled_qty == 3
    assert order.state is not FILLED and order.state == PARTIAL
    assert order.remaining == 2
    # weighted average of what was actually paid: (2*1.00 + 1*1.30) / 3
    assert order.avg_price == pytest.approx((2 * 1.00 + 1 * 1.30) / 3)
    position = book.open_positions()[0]
    assert position.contracts == 3
    assert position.avg_price == pytest.approx((2 * 1.00 + 1 * 1.30) / 3)


def test_an_identical_ack_cannot_double_fill():
    """A redelivered broker response is the same answer, not a second fill."""
    book = ExecutionBook()
    order = book.record_intent(intent(qty=5, limit=1.00), ts=T0)
    book.apply_ack(order.client_id, ack(3, 1.00), ts=T0)
    book.apply_ack(order.client_id, ack(3, 1.00), ts=T0 + 1)
    book.apply_ack(order.client_id, ack(3, 1.00), ts=T0 + 2)

    assert order.filled_qty == 3
    assert len(book.fills) == 1
    assert book.duplicates == 2
    assert book.open_positions()[0].contracts == 3


def test_a_completed_order_ignores_a_repeated_full_ack():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=2, limit=1.00), ts=T0)
    book.apply_ack(order.client_id, ack(2, 1.00), ts=T0)
    assert order.state == FILLED
    book.apply_ack(order.client_id, ack(2, 1.00), ts=T0 + 5)
    assert order.filled_qty == 2
    assert len(book.fills) == 1
    assert book.realized_total == 0.0


def test_a_regressing_ack_is_refused_rather_than_clamped():
    """A smaller cumulative than already applied means a reused id or a corrupt log."""
    book = ExecutionBook()
    order = book.record_intent(intent(qty=5, limit=1.00), ts=T0)
    book.apply_ack(order.client_id, ack(3, 1.00), ts=T0)
    with pytest.raises(ExecutionBookError, match="regressed"):
        book.apply_ack(order.client_id, ack(2, 1.00), ts=T0 + 1)
    assert order.filled_qty == 3
    assert book.open_positions()[0].contracts == 3


def test_an_ack_above_the_intent_is_clamped_and_counted():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=2, limit=1.00), ts=T0)
    book.apply_ack(order.client_id, ack(9, 1.00), ts=T0)
    assert order.filled_qty == 2
    assert order.state == FILLED
    assert book.anomalies["ack_qty_exceeds_intent"] == 1


def test_an_ack_for_a_different_contract_is_refused():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=2, limit=1.00), ts=T0)
    with pytest.raises(ExecutionBookError, match="ack contract"):
        book.apply_ack(order.client_id, ack(2, 1.00, key=NVDA), ts=T0)
    assert order.filled_qty == 0


def test_a_missing_ack_price_uses_the_limit_and_says_so():
    """A zero price is a missing field, not a free contract."""
    book = ExecutionBook()
    order = book.record_intent(intent(qty=2, limit=1.25), ts=T0)
    book.apply_ack(order.client_id, ack(2, 0.0), ts=T0)
    assert order.avg_price == pytest.approx(1.25)
    assert book.anomalies["ack_price_missing"] == 1
    assert book.open_positions()[0].avg_price == pytest.approx(1.25)


# ── rejects and partial outcomes ─────────────────────────────────────────────
def test_a_business_reject_never_fills_and_never_holds_a_position():
    """The rejected buy used to stay in the engine's book and hold a slot forever."""
    book = ExecutionBook()
    order = book.record_intent(intent(qty=5, limit=1.00), ts=T0)
    book.apply_ack(order.client_id, ack(0, 0.0, ok=False,
                                        error="insufficient_buying_power"), ts=T0)
    assert order.state == REJECTED
    assert order.error == "insufficient_buying_power"
    assert book.open_positions() == []
    assert book.fills == []
    assert book.realized_total == 0.0


def test_a_reject_after_a_partial_fill_cancels_and_keeps_what_it_got():
    """A fill outranks a late reject: the venue did execute, whatever came after."""
    book = ExecutionBook()
    order = book.record_intent(intent(qty=5, limit=1.00), ts=T0)
    book.apply_ack(order.client_id, ack(2, 1.00), ts=T0)
    book.reject(order.client_id, reason="cancelled_by_user", ts=T0 + 1)
    assert order.state == CANCELLED
    assert order.filled_qty == 2
    assert book.open_positions()[0].contracts == 2
    assert book.problems() == []


def test_a_late_reject_cannot_undo_a_full_fill():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=2, limit=1.00), ts=T0)
    book.apply_ack(order.client_id, ack(2, 1.00), ts=T0)
    book.reject(order.client_id, reason="stale_cancel", ts=T0 + 1)
    assert order.state == FILLED
    assert book.open_positions()[0].contracts == 2


def test_a_fill_after_a_terminal_is_refused():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=2, limit=1.00), ts=T0)
    book.reject(order.client_id, reason="no_buying_power", ts=T0)
    with pytest.raises(ExecutionBookError, match="fill after terminal"):
        book.record_fill(order.client_id, qty=1, price=1.0, ts=T0 + 1)


# ── sells: clamp, never invent a short ───────────────────────────────────────
def test_a_sell_larger_than_the_lot_is_clamped_and_counted():
    book = ExecutionBook()
    fill_buy(book, 5, 1.00)
    sell = book.record_intent(intent("sell", qty=7, limit=1.20), ts=T0 + 1)
    book.apply_ack(sell.client_id, ack(7, 1.20, broker_id="rh-2"), ts=T0 + 1)

    assert book.realized_total == 100.0                    # (1.20-1.00)*5*100
    assert len(book.realized) == 1
    assert book.realized[0].qty == 5
    assert book.open_positions() == []
    assert book.anomalies["oversell_clamped"] == 1
    assert book.problems() == []


def test_a_sell_with_no_lot_invents_nothing():
    """This book is long premium only: an unheld sell is an anomaly, not a short."""
    book = ExecutionBook()
    sell = book.record_intent(intent("sell", qty=2, limit=1.20, key=NVDA), ts=T0)
    book.apply_ack(sell.client_id, ack(2, 1.20, key=NVDA), ts=T0)
    assert book.realized == []
    assert book.realized_total == 0.0
    assert book.open_positions() == []
    assert book.anomalies["sell_without_lot"] == 1
    # The intent is still terminal: the venue answered, the book simply holds nothing.
    assert sell.state == FILLED
    assert book.unresolved_orders() == []


def test_a_fill_with_no_intent_is_refused():
    """A venue execution this book never recorded is an adoption, not an auto-create."""
    book = ExecutionBook()
    with pytest.raises(ExecutionBookError, match="unknown order"):
        book.record_fill("QQQ|CALL|720|0DTE:buy:1@2026-09-08", qty=1, price=1.0, ts=T0)
    with pytest.raises(ExecutionBookError, match="unknown order"):
        book.apply_ack("nope", ack(1), ts=T0)


def test_a_contract_key_that_cannot_name_an_option_is_refused_at_intent_time():
    book = ExecutionBook()
    with pytest.raises(ExecutionBookError, match="not a contract key"):
        book.record_intent(intent(qty=1, key="QQQ"), ts=T0)
    assert book.orders == {}


def test_an_intent_without_an_idempotency_key_is_refused():
    """Two identical orders must never collapse into one client id."""
    book = ExecutionBook()
    with pytest.raises(ExecutionBookError, match="idempotency key"):
        book.record_intent(OrderIntent(action="buy", qty=2, limit=1.0,
                                       contract_key=QQQ, reason="entry"), ts=T0)


def test_an_intent_must_ask_for_at_least_one_contract():
    book = ExecutionBook()
    with pytest.raises(ExecutionBookError, match="at least one contract"):
        book.record_intent(intent(qty=0), ts=T0)
    with pytest.raises(ExecutionBookError, match="unknown action"):
        book.record_intent(intent(action="hold", qty=1), ts=T0)


def test_the_same_intent_twice_is_one_order_and_a_conflict_is_refused():
    book = ExecutionBook()
    first = book.record_intent(intent(qty=2, idem="QQQ:buy:1"), ts=T0)
    again = book.record_intent(intent(qty=2, idem="QQQ:buy:1"), ts=T0 + 1)
    assert again.client_id == first.client_id
    assert len(book.orders) == 1
    assert book.duplicates == 1
    with pytest.raises(ExecutionBookError, match="duplicate client id"):
        book.record_intent(intent(qty=3, idem="QQQ:buy:1"), ts=T0 + 2)


def test_the_client_id_carries_the_session_so_a_0dte_key_is_not_reused():
    """The same 0DTE contract legitimately trades again tomorrow."""
    day_two = T0 + 86_400.0
    assert utc_day(day_two) != DAY
    book = ExecutionBook()
    first = book.record_intent(intent(qty=1, idem="QQQ:buy:1"), ts=T0)
    second = book.record_intent(intent(qty=1, idem="QQQ:buy:1"), ts=day_two)
    assert first.client_id != second.client_id
    assert len(book.orders) == 2


# ── the log: replay, torn lines, corruption ──────────────────────────────────
def test_restart_replays_the_same_book(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    fill_buy(book, 5, 1.00)
    sell = book.record_intent(intent("sell", qty=2, limit=1.40, reason="trim:runners"),
                              ts=T0 + 1)
    book.apply_ack(sell.client_id, ack(2, 1.40, broker_id="rh-2"), ts=T0 + 1)

    again = ExecutionBook.load(path)
    assert again.snapshot() == book.snapshot()
    assert again.digest() == book.digest()
    assert again.realized_total == book.realized_total
    assert [p.contract_key for p in again.open_positions()] == [QQQ]
    assert again.open_positions()[0].contracts == 3
    assert again.closed_keys(DAY) == set()


def test_a_torn_final_line_is_ignored_and_counted(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    fill_buy(book, 3, 1.00)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"v":1,"t":"intent","cid":"torn')      # a crash mid-append

    again = ExecutionBook.load(path)
    assert again.torn_lines == 1
    assert again.open_positions()[0].contracts == 3
    assert again.snapshot()["orders"] == 1


def test_corruption_in_the_middle_is_refused(tmp_path):
    """A torn LAST line is a crash; a torn MIDDLE line is lost history."""
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    fill_buy(book, 3, 1.00)
    fill_buy(book, 1, 2.00, key=NVDA)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(1, '{"v":1,"t":"intent","cid":')            # unreadable, not last
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ExecutionBookError, match="corrupt"):
        ExecutionBook.load(path)


def test_an_unknown_schema_version_is_refused(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    fill_buy(book, 1, 1.00)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["v"] = 2
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    with pytest.raises(ExecutionBookError, match="schema version"):
        ExecutionBook.load(path)


def test_a_line_that_is_not_an_object_is_refused(tmp_path):
    path = tmp_path / "execution.jsonl"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ExecutionBookError, match="not an object"):
        ExecutionBook.load(path)


def test_a_missing_log_is_an_empty_book(tmp_path):
    book = ExecutionBook.load(tmp_path / "nope.jsonl")
    assert book.orders == {}
    assert book.open_positions() == []
    assert book.torn_lines == 0
    assert book.problems() == []


def test_an_event_kind_the_build_does_not_know_is_refused(tmp_path):
    path = tmp_path / "execution.jsonl"
    path.write_text('{"v":1,"t":"teleport","cid":"x"}\n', encoding="utf-8")
    with pytest.raises(ExecutionBookError, match="unknown execution event"):
        ExecutionBook.load(path)


# ── restart mid-send: unresolved, never re-sent ──────────────────────────────
def test_a_restart_mid_send_leaves_the_order_unresolved(tmp_path):
    """The send may have reached the venue. The book must not pretend to know."""
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    order = book.record_intent(intent(qty=5), ts=T0)
    book.mark_unknown(order.client_id, reason="transport:read timeout", ts=T0)

    again = ExecutionBook.load(path)
    assert again.open_positions() == []                    # nothing was confirmed
    assert len(again.unresolved_orders()) == 1
    assert again.unknown_orders()[0].last_unknown == "transport:read timeout"
    assert again.unknown_orders()[0].state == PENDING
    assert again.problems() == []                          # an open order is not corruption


def test_recover_settles_an_unknown_order_from_the_venue_answer(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    order = book.record_intent(intent(qty=5), ts=T0)
    book.mark_unknown(order.client_id, reason="transport:reset", ts=T0)

    again = ExecutionBook.load(path)
    recovered = again.unresolved_orders()[0]
    again.apply_ack(recovered.client_id, ack(5, 1.00), ts=T0 + 60)
    assert again.unresolved_orders() == []
    assert again.unknown_orders() == []
    assert again.open_positions()[0].contracts == 5
    assert again.orders[recovered.client_id].last_unknown == ""


def test_a_not_seen_answer_closes_the_unknown_order_with_no_position(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    order = book.record_intent(intent(qty=5), ts=T0)
    book.mark_unknown(order.client_id, reason="transport:reset", ts=T0)
    book.reject(order.client_id, reason="recover:not_seen", ts=T0 + 60)

    again = ExecutionBook.load(path)
    assert again.unresolved_orders() == []
    assert again.open_positions() == []
    assert again.orders[order.client_id].state == REJECTED


def test_unknown_after_a_terminal_is_refused():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=1), ts=T0)
    book.apply_ack(order.client_id, ack(1, 1.00), ts=T0)
    with pytest.raises(ExecutionBookError, match="after terminal"):
        book.mark_unknown(order.client_id, reason="late failure", ts=T0 + 1)


# ── baseline + sync_engine: the restart is a replay ─────────────────────────
def test_sync_engine_restores_positions_and_does_not_erase_a_drawdown():
    """The book is the authority; the engine's positions were only a decision cache."""
    now = T0 + 60
    engine = Engine(fills=FillLog())
    day = engine.trading_day(now)
    book = ExecutionBook(day_of=lambda ts: day)
    book.ensure_baseline(equity_usd=10_000.0, peak_equity_usd=12_000.0, day=day,
                         realized_today_usd=-500.0, trades_today=3,
                         consecutive_losses=2, ts=now)
    fill_buy(book, 4, 1.00)
    sell = book.record_intent(intent("sell", qty=4, limit=0.50), ts=now)
    book.apply_ack(sell.client_id, ack(4, 0.50, broker_id="rh-2"), ts=now)
    assert book.realized_total == -200.0                    # (0.50-1.00)*4*100

    report = book.sync_engine(engine, now)

    assert report.day == day
    assert report.trades_today == 1
    assert report.entries_today == 1
    # baseline 10,000 plus the confirmed -200: the drawdown survives the restart.
    assert engine.stats.realized_usd == 9_800.0
    assert engine.governor_state.equity_usd == 9_800.0
    assert engine.governor_state.peak_equity_usd == 12_000.0
    assert engine.governor_state.drawdown_usd == 2_200.0
    assert engine.governor_state.realized_today_usd == pytest.approx(-700.0)
    assert engine.governor_state.consecutive_losses == 3    # inherited 2 + this close
    assert engine.day == day
    # The sync is auditable in the fill log, with the digest that produced it.
    sync_rows = [f for f in engine.fills.records if f.event == "sync"]
    assert len(sync_rows) == 1
    assert book.digest()[:16] in sync_rows[0].note


def test_sync_engine_replaces_the_decision_cache_with_confirmed_state():
    now = T0 + 60
    engine = Engine(fills=FillLog())
    day = engine.trading_day(now)
    engine.positions = {QQQ: Position(contract_key=QQQ, ticker="QQQ", side=Side.CALL,
                                      contracts=5, avg_price=1.0, opened_ts=now)}
    engine.stats.trades_today = 3
    book = ExecutionBook(day_of=lambda ts: day)
    book.ensure_baseline(equity_usd=0.0, peak_equity_usd=0.0, day=day,
                         realized_today_usd=0.0, trades_today=0,
                         consecutive_losses=0, ts=now)

    book.sync_engine(engine, now)
    # The engine thought it held 5 contracts it never had confirmed.
    assert engine.positions == {}
    assert engine.stats.trades_today == 0


def test_sync_engine_restores_closed_contracts_so_a_restart_cannot_re_chase():
    now = T0 + 60
    engine = Engine(fills=FillLog())
    day = engine.trading_day(now)
    book = ExecutionBook(day_of=lambda ts: day)
    fill_buy(book, 2, 1.00)
    sell = book.record_intent(intent("sell", qty=2, limit=1.10), ts=T0)
    book.apply_ack(sell.client_id, ack(2, 1.10, broker_id="rh-2"), ts=T0)

    book.sync_engine(engine, now)
    assert engine.closed == {QQQ}
    assert engine.positions == {}


def test_sync_engine_restores_the_pipe_test_counter_but_not_the_adds():
    now = T0 + 60
    engine = Engine(fills=FillLog())
    day = engine.trading_day(now)
    book = ExecutionBook(day_of=lambda ts: day)
    fill_buy(book, 1, 1.00)
    fill_buy(book, 1, 1.05)                                  # a second entry
    added = book.record_intent(intent(qty=1, limit=1.10, reason="add:new avg"), ts=T0 + 1)
    book.apply_ack(added.client_id, ack(1, 1.10, broker_id="rh-3"), ts=T0 + 1)

    report = book.sync_engine(engine, now)
    assert report.entries_today == 2                         # the add is not an entry
    assert engine.stats.trades_today == 2
    assert book.open_positions()[0].contracts == 3


def test_sync_engine_contains_unresolved_orders_in_its_report():
    now = T0 + 60
    engine = Engine(fills=FillLog())
    day = engine.trading_day(now)
    book = ExecutionBook(day_of=lambda ts: day)
    order = book.record_intent(intent(qty=1), ts=T0)
    book.mark_unknown(order.client_id, reason="transport:reset", ts=T0)
    report = book.sync_engine(engine, now)
    assert report.unresolved == 1
    assert engine.positions == {}


def test_ensure_baseline_is_written_once_and_survives_a_reload(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    first = book.ensure_baseline(equity_usd=10_000.0, peak_equity_usd=11_000.0,
                                 day=DAY, realized_today_usd=-250.0, trades_today=2,
                                 consecutive_losses=1, ts=T0)
    second = book.ensure_baseline(equity_usd=1.0, peak_equity_usd=1.0, day="2027-01-01",
                                  realized_today_usd=0.0, trades_today=0,
                                  consecutive_losses=0, ts=T0 + 1)
    assert second is first and first.equity_usd == 10_000.0

    again = ExecutionBook.load(path)
    assert again.baseline is not None
    assert again.baseline.peak_equity_usd == 11_000.0
    assert again.baseline.day == DAY
    assert len([line for line in path.read_text(encoding="utf-8").splitlines()
                if '"t":"baseline"' in line]) == 1


# ── expiry: a dead contract must not hold a slot ────────────────────────────
def test_a_carried_0dte_lot_expires_at_zero_on_the_next_day():
    book = ExecutionBook(day_of=utc_day)
    fill_buy(book, 2, 1.50)                                   # 2 * $1.50 * 100 = $300
    tomorrow = T0 + 86_400.0

    assert book.expire_carried(utc_day(tomorrow), tomorrow) == 1
    assert book.open_positions() == []
    assert book.realized_total == -300.0
    assert book.anomalies["expired_at_zero"] == 1
    assert book.orders[list(book.orders)[0]].state == FILLED
    # the release is a named event in the book's own log, not a silent delete
    assert book.fills[-1].action == "expire"
    assert book.closed_keys(utc_day(tomorrow)) == {QQQ}


def test_an_iso_dated_lot_expires_only_after_its_date():
    book = ExecutionBook(day_of=utc_day)
    fill_buy(book, 1, 1.00, key=NVDA)                        # expiry 2026-09-11
    assert book.expire_carried("2026-09-10", T0) == 0
    assert len(book.open_positions()) == 1
    assert book.expire_carried("2026-09-12", T0) == 1
    assert len(book.open_positions()) == 0


def test_an_unreadable_expiry_is_not_expired():
    """'?' is not evidence that the contract is dead; guessing would delete risk."""
    book = ExecutionBook(day_of=utc_day)
    fill_buy(book, 1, 1.00, key="NVDA|CALL|230|?")
    assert book.expire_carried("2027-01-01", T0) == 0
    assert len(book.open_positions()) == 1
    assert book.anomalies["expiry_unreadable"] == 1


def test_expiry_without_an_assumption_releases_the_slot_without_inventing_pnl():
    book = ExecutionBook(day_of=utc_day)
    fill_buy(book, 2, 1.50)
    tomorrow = T0 + 86_400.0
    assert book.expire_carried(utc_day(tomorrow), tomorrow, assume_zero=False) == 1
    assert book.open_positions() == []
    assert book.realized_total == 0.0
    assert book.realized == []
    assert book.anomalies["expired_unsettled"] == 1


def test_sync_engine_expires_a_lot_the_market_already_took():
    now = T0 + 3 * 86_400.0
    engine = Engine(fills=FillLog())
    book = ExecutionBook(day_of=utc_day)
    fill_buy(book, 1, 2.00)                                  # opened three days earlier
    report = book.sync_engine(engine, now)
    assert report.expired == 1
    assert engine.positions == {}
    assert engine.governor_state.equity_usd == -200.0


# ── trim / add: the lot learns what happened to it ──────────────────────────
def test_a_trim_arms_the_breakeven_stop_on_the_confirmed_lot():
    book = ExecutionBook()
    fill_buy(book, 5, 1.00)
    trim = book.record_intent(intent("sell", qty=4, limit=1.30, reason="trim:runners at $3"),
                              ts=T0 + 1)
    book.apply_ack(trim.client_id, ack(4, 1.30, broker_id="rh-2"), ts=T0 + 1)

    lot = book.open_positions()[0]
    assert lot.contracts == 1
    assert lot.trimmed == 1
    assert lot.breakeven_stop == pytest.approx(1.00)
    assert book.realized_total == 120.0


def test_an_add_is_counted_on_the_lot_and_averaged_into_its_price():
    book = ExecutionBook()
    fill_buy(book, 2, 1.00)
    added = book.record_intent(intent(qty=2, limit=1.20, reason="add:adding at 1.20"),
                               ts=T0 + 1)
    book.apply_ack(added.client_id, ack(2, 1.20, broker_id="rh-2"), ts=T0 + 1)

    lot = book.open_positions()[0]
    assert lot.contracts == 4
    assert lot.adds == 1
    assert lot.avg_price == pytest.approx((2 * 1.00 + 2 * 1.20) / 4)
    assert book.entries_on(DAY) == 1                         # the add is not an entry


# ── verify: the log states what it can prove ────────────────────────────────
def test_verify_names_a_realised_total_that_does_not_match_its_trades():
    book = ExecutionBook()
    fill_buy(book, 2, 1.00)
    sell = book.record_intent(intent("sell", qty=2, limit=1.50), ts=T0 + 1)
    book.apply_ack(sell.client_id, ack(2, 1.50, broker_id="rh-2"), ts=T0 + 1)
    assert book.problems() == []

    book.realized_total += 1.0                               # a corrupted total
    problems = book.problems()
    assert len(problems) == 1
    assert "realised total" in problems[0]


def test_verify_names_a_lot_that_is_already_closed():
    book = ExecutionBook()
    fill_buy(book, 2, 1.00)
    lot = book.lots[QQQ]
    lot.qty = 0
    problems = book.problems()
    assert any("closed lot still on the book" in problem for problem in problems)


def test_verify_does_not_call_an_in_flight_order_a_defect():
    book = ExecutionBook()
    book.record_intent(intent(qty=1), ts=T0)
    assert book.problems() == []
    assert len(book.unresolved_orders()) == 1


def test_the_snapshot_is_order_independent_and_canonical():
    book = ExecutionBook()
    fill_buy(book, 2, 1.00, key=QQQ)
    fill_buy(book, 1, 2.00, key=NVDA)
    snap = book.snapshot()
    assert list(snap["positions"]) == sorted([QQQ, NVDA])
    assert snap["contracts"] == 3
    assert snap["schema"].startswith("mff.execution-book.v")
    assert json.loads(json.dumps(snap)) == snap


# ── the TCA seam: real fills, not estimates ─────────────────────────────────
def test_execution_rows_export_one_row_per_filled_order_with_the_reference():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=5, limit=1.05), ts=T0)
    book.apply_ack(order.client_id, ack(3, 1.10), ts=T0 + 2)
    book.reject(order.client_id, reason="cancelled", ts=T0 + 3)

    rows = book.execution_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.qty == 5 and row.fill_qty == 3
    assert row.fill_ratio == pytest.approx(0.6)              # a partial, measured
    assert row.action == "buy" and row.contract_key == QQQ
    assert row.reference == pytest.approx(1.00)              # his posted bid, recorded
    assert row.latency_ms == pytest.approx(2_000.0)
    assert row.cost_per_contract == pytest.approx(0.05)      # paid 1.10 against 1.05


def test_execution_rows_exclude_orders_that_never_filled():
    book = ExecutionBook()
    order = book.record_intent(intent(qty=1), ts=T0)
    book.reject(order.client_id, reason="no_buying_power", ts=T0)
    assert book.execution_rows() == []


# ── the listener: confirmed execution is wired into the hot path ────────────
class RecordingBroker:
    def __init__(self, *, fail: Exception | None = None, qty: int | None = None) -> None:
        self.fail = fail
        self.qty = qty
        self.placed: list[OrderIntent] = []

    def place(self, intent: OrderIntent) -> Ack:
        self.placed.append(intent)
        if self.fail is not None:
            raise self.fail
        return Ack(ok=True, contract_key=intent.contract_key,
                   qty=intent.qty if self.qty is None else self.qty,
                   price=intent.limit, broker_order_id="rh-live-1")


def build_listener(tmp_path, broker, execution, text, **cfg):
    class Quotes:
        def quote(self, contract_key: str) -> Quote | None:
            return Quote(bid=1.01, ask=1.05)

    listener = Listener(
        transport=TapeTransport([]),
        engine=Engine(config=EngineConfig(**cfg), fills=FillLog(), clock=lambda: T0),
        broker=broker,
        quotes=Quotes(),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=lambda: T0),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: T0),
        clock=lambda: T0,
        execution=execution,
    )
    from scorpion_mff.types import RawMessage

    listener.handle(RawMessage(channel="etf-options", channel_id="1231301953972207667",
                               ts=T0, text=text))
    return listener


ENTRY = "CALL TICKER QQQ STRIKE $720 Exp Today Current BID Price $1.01"


def test_the_listener_books_the_intent_and_the_confirmed_fill(tmp_path):
    book = ExecutionBook()
    listener = build_listener(tmp_path, PaperBroker(), book, ENTRY)

    assert len(book.orders) == 1
    order = list(book.orders.values())[0]
    assert order.action == "buy" and order.filled_qty == order.intent.qty
    assert order.state == FILLED
    assert order.channel == "etf-options"
    position = listener.engine.open_positions[0]
    assert book.open_positions()[0].contract_key == position.contract_key
    assert book.open_positions()[0].contracts == position.contracts


def test_a_transport_failure_leaves_the_book_order_open_and_names_the_cause(tmp_path):
    book = ExecutionBook()
    build_listener(tmp_path, RecordingBroker(fail=TransportError("socket reset")),
                   book, ENTRY)

    order = list(book.orders.values())[0]
    assert order.state == PENDING
    assert "socket reset" in order.last_unknown
    assert book.open_positions() == []                       # nothing was confirmed
    assert [o.client_id for o in book.unknown_orders()] == [order.client_id]


def test_a_business_reject_never_reaches_the_confirmed_book_as_a_holding(tmp_path):
    book = ExecutionBook()
    build_listener(tmp_path, RecordingBroker(fail=BrokerError("no_buying_power")),
                   book, ENTRY)
    order = list(book.orders.values())[0]
    assert order.state == REJECTED
    assert "no_buying_power" in order.error
    assert book.open_positions() == []
    assert book.problems() == []


def test_a_partial_ack_from_the_listener_is_a_partial_position(tmp_path):
    """The venue filled 1 of a full clip: the book holds 1 and says it is partial."""
    book = ExecutionBook()
    build_listener(tmp_path, RecordingBroker(qty=1), book, ENTRY, pipe_test=False)
    order = list(book.orders.values())[0]
    assert order.intent.qty > 1                              # not the one-lot pipe test
    assert order.filled_qty == 1
    assert order.state == PARTIAL
    assert order.remaining == order.intent.qty - 1
    assert book.open_positions()[0].contracts == 1
    assert book.problems() == []


def test_a_book_that_cannot_write_is_paged_not_fatal(tmp_path):
    """A bookkeeping failure must not abort an order that already left the box."""
    book = ExecutionBook(path=tmp_path / "execution.jsonl")

    def broken(*args, **kwargs):
        raise ExecutionBookError("disk full")

    book.record_intent = broken                              # type: ignore[method-assign]
    listener = build_listener(tmp_path, PaperBroker(), book, ENTRY)
    alerts = [f for f in listener.engine.fills.records if f.event == "alert"]
    assert any("execution_book:intent" in a.reason for a in alerts)
    # the venue's answer is still in the fill log, and the engine still holds the clip
    assert any(f.event == "ack" for f in listener.engine.fills.records)


# ── the CLI: the operator surface over the same book ────────────────────────
def test_cli_book_show_prints_the_confirmed_book(tmp_path, capsys):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    fill_buy(book, 3, 1.00)
    assert cli.main(["book", "show", "--path", str(path)]) == 0
    out = capsys.readouterr().out
    assert "execution book" in out
    assert "3 contract(s)" in out
    assert book.digest()[:16] in out


def test_cli_book_replay_proves_determinism(tmp_path, capsys):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    fill_buy(book, 2, 1.00)
    assert cli.main(["book", "replay", "--path", str(path)]) == 0
    assert "IDENTICAL" in capsys.readouterr().out


def test_cli_book_verify_strict_fails_on_an_unresolved_order(tmp_path, capsys):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    order = book.record_intent(intent(qty=1), ts=T0)
    book.mark_unknown(order.client_id, reason="transport:reset", ts=T0)

    assert cli.main(["book", "verify", "--path", str(path)]) == 0
    assert "unresolved" in capsys.readouterr().out.lower()
    assert cli.main(["book", "verify", "--path", str(path), "--strict"]) == 1


def test_cli_book_verify_refuses_a_corrupt_log(tmp_path, capsys):
    path = tmp_path / "execution.jsonl"
    path.write_text('{"v":1,"t":"intent","cid":"a"}\n{"v":1,"t":"nope"}\nbogus\n',
                    encoding="utf-8")
    assert cli.main(["book", "verify", "--path", str(path)]) == 2
    assert "cannot use the execution log" in capsys.readouterr().err


def test_cli_book_sync_restores_a_fresh_engine(tmp_path, capsys):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    fill_buy(book, 2, 1.00)
    assert cli.main(["book", "sync", "--path", str(path), "--now", str(T0)]) == 0
    out = capsys.readouterr().out
    assert "restored 1 position(s)" in out
    assert QQQ in out


def test_cli_book_export_writes_tca_rows_and_refuses_to_overwrite(tmp_path, capsys):
    path = tmp_path / "execution.jsonl"
    target = tmp_path / "executions.jsonl"
    book = ExecutionBook(path=path)
    fill_buy(book, 2, 1.00)

    assert cli.main(["book", "export", "--path", str(path),
                     "--executions", str(target)]) == 0
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["contract_key"] == QQQ
    assert rows[0]["fill_qty"] == 2
    capsys.readouterr()

    assert cli.main(["book", "export", "--path", str(path),
                     "--executions", str(target)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert cli.main(["book", "export", "--path", str(path),
                     "--executions", str(target), "--replace"]) == 0


def test_cli_book_export_with_no_fills_is_not_an_error(tmp_path, capsys):
    path = tmp_path / "execution.jsonl"
    path.write_text("", encoding="utf-8")
    assert cli.main(["book", "export", "--path", str(path),
                     "--executions", str(tmp_path / "e.jsonl")]) == 0
    assert "nothing to export" in capsys.readouterr().out


def test_cli_book_show_json_is_machine_readable(tmp_path, capsys):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    fill_buy(book, 1, 1.00)
    assert cli.main(["book", "show", "--path", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["book"]["contracts"] == 1
    assert payload["digest"] == book.digest()
    assert payload["unresolved"] == []


# ── the small helpers, pinned ───────────────────────────────────────────────
def test_contract_parts_reads_the_stack_identity():
    ticker, side, expiry = contract_parts("QQQ|CALL|720|0DTE")
    assert (ticker, side, expiry) == ("QQQ", Side.CALL, "0DTE")
    ticker, side, expiry = contract_parts("NVDA|PUT|227.5|2026-09-11")
    assert (ticker, side, expiry) == ("NVDA", Side.PUT, "2026-09-11")
    _, side, expiry = contract_parts("QQQ")
    assert side is None and expiry == ""


def test_the_log_is_one_json_object_per_line_with_a_schema_version(tmp_path):
    path = tmp_path / "execution.jsonl"
    book = ExecutionBook(path=path)
    fill_buy(book, 1, 1.00)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2                                   # intent + ack
    for line in lines:
        row = json.loads(line)
        assert row["v"] == 1
        assert set(row) >= {"v", "t", "cid", "ts"}
        assert line == json.dumps(row, sort_keys=True, separators=(",", ":"))


def test_an_unwritable_log_does_not_parse_as_an_empty_book(tmp_path):
    """A directory where the log should be is an error, never 'no executions'."""
    path = Path(tmp_path) / "execution.jsonl"
    path.mkdir()
    with pytest.raises(ExecutionBookError, match="cannot read execution log"):
        ExecutionBook.load(path)

"""The running engine follows confirmed execution, not its own intent.

`ExecutionBook.sync_engine` already made a restart honest. This suite is about the
process that never restarts: a sell is booked at *intent* time, so before this join a
sell the venue rejected, partially filled, or never answered left the bot believing it
was flat - with the contract in `closed`, so "never chase" refused to exit again. The
lot was still at the venue and invisible to every guard.

The invariant: **the engine is never more optimistic than the venue's last answer**, and
**one exit is in flight per contract**. The two asymetries are deliberate:

* an unknown SELL restores the position (the risk may still exist);
* an unknown BUY leaves the engine's position alone (the exposure may still exist).

Every test is offline and deterministic; no order leaves the process.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from scorpion_mff.broker import Ack, StaticQuotes, TransportError
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.execution_book import FILLED, PARTIAL, PENDING, REJECTED, ExecutionBook
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.types import OrderIntent, RawMessage

T0 = 1_757_340_000.0                       # 2025-09-08 10:00 ET (a regular session)
CH = "etf-options"
CH_ID = "1231301953972207667"
QQQ = "QQQ|CALL|720|0DTE"
ENTRY = "CALL TICKER QQQ STRIKE $720 Exp Today Current BID Price $1.01"
CLOSE = "QQQ CALLs closed at 1.50"


@dataclass
class Clock:
    """One clock shared by the engine and the listener, so event order is exact."""

    now: float = T0

    def __call__(self) -> float:
        return self.now

    def set(self, ts: float) -> None:
        self.now = ts


@dataclass
class ScriptedBroker:
    """A venue with a script: each `place` consumes one answer.

    An answer is `(ok, qty, error)` where `qty=None` means "the full intent", or an
    exception instance to raise. The broker records what it was asked to do, which is
    how the tests prove a second exit was never *sent*.
    """

    answers: list[object]
    placed: list[OrderIntent] = field(default_factory=list)
    placed_keys: set[str] = field(default_factory=set)

    def place(self, intent: OrderIntent) -> Ack:
        self.placed.append(intent)
        self.placed_keys.add(intent.idempotency_key or intent.contract_key)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        ok, qty, error, *price = answer
        return Ack(ok=ok, contract_key=intent.contract_key,
                   qty=intent.qty if qty is None else qty,
                   price=intent.limit if not price else price[0],
                   broker_order_id=f"rh-{len(self.placed)}", error=error)

    def lookup(self, idempotency_key: str):
        from scorpion_mff.broker import OrderLookup

        return (OrderLookup.SEEN if idempotency_key in self.placed_keys
                else OrderLookup.NOT_SEEN)


@dataclass
class Harness:
    listener: Listener
    book: ExecutionBook
    broker: ScriptedBroker
    clock: Clock

    @property
    def engine(self) -> Engine:
        return self.listener.engine


def build(tmp_path, *answers: object, enforce_expiry: bool = False) -> Harness:
    clock = Clock()
    broker = ScriptedBroker(answers=list(answers))
    book = ExecutionBook()
    engine = Engine(config=EngineConfig(pipe_test=False, enforce_expiry=enforce_expiry),
                    fills=FillLog(), clock=clock)
    listener = Listener(
        transport=TapeTransport([]),
        engine=engine,
        broker=broker,
        quotes=StaticQuotes({QQQ: (1.01, 1.05)}),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=clock),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=clock),
        clock=clock,
        execution=book,
    )
    return Harness(listener=listener, book=book, broker=broker, clock=clock)


def send(harness: Harness, ts: float, text: str) -> list[OrderIntent]:
    harness.clock.set(ts)
    return harness.listener.handle(
        RawMessage(channel=CH, channel_id=CH_ID, text=text, ts=ts))


def enter(harness: Harness, ts: float = T0) -> int:
    intents = send(harness, ts, ENTRY)
    assert len(intents) == 1 and intents[0].action == "buy"
    return intents[0].qty


def reasons(harness: Harness, event: str | None = None) -> list[str]:
    return [row.reason for row in harness.engine.fills.records
            if event is None or row.event == event]


# ── a refused sell is visible, and can be exited again ──────────────────────
def test_a_rejected_sell_restores_the_position_and_a_retry_is_a_new_order(tmp_path):
    harness = build(tmp_path,
                    (True, None, ""),                       # the entry fills
                    (False, 0, "position_not_found"),       # the close is refused
                    (True, None, ""))                       # the retry fills
    qty = enter(harness)

    send(harness, T0 + 60, CLOSE)
    # The venue said no, so the engine must still hold what it holds.
    position = harness.engine.positions[QQQ]
    assert position.contracts == qty
    assert harness.engine.exit_in_flight == set()
    assert harness.book.open_positions()[0].contracts == qty
    assert list(harness.book.orders.values())[-1].state == REJECTED
    assert "engine_follows_venue" in reasons(harness, "reconcile")

    intents = send(harness, T0 + 120, CLOSE)
    assert [i.action for i in intents] == ["sell"]
    # A new attempt is a new idempotency key. Reusing the refused key would ask the
    # venue to replay its refusal, and could collapse two genuine orders into one.
    assert harness.broker.placed[1].idempotency_key != \
        harness.broker.placed[2].idempotency_key
    assert harness.engine.open_positions == []
    assert harness.book.open_positions() == []
    assert harness.book.realized_total == pytest.approx((1.50 - 1.05) * qty * 100)
    assert harness.engine.exit_in_flight == set()


def test_a_partial_sell_leaves_the_remainder_visible_and_blocks_a_second_exit(tmp_path):
    harness = build(tmp_path,
                    (True, None, ""),                       # the entry fills
                    (True, 1, ""))                          # the close fills one
    qty = enter(harness)
    assert qty >= 2

    send(harness, T0 + 60, CLOSE)
    order = list(harness.book.orders.values())[-1]
    assert order.state == PARTIAL
    assert harness.engine.positions[QQQ].contracts == qty - 1
    assert harness.engine.exit_in_flight == {QQQ}
    assert harness.book.open_positions()[0].contracts == qty - 1

    # A second close is not a retry: the first sell is still at the venue.
    intents = send(harness, T0 + 120, CLOSE)
    assert intents == []
    assert "close_while_exiting" in reasons(harness, "skip")
    assert len(harness.broker.placed) == 2


def test_the_expiry_guard_pages_instead_of_selling_twice(tmp_path):
    harness = build(tmp_path,
                    (True, None, ""),
                    (True, 1, ""),                          # partial: the rest is open
                    enforce_expiry=True)
    qty = enter(harness)
    send(harness, T0 + 60, CLOSE)

    # 15:45 ET on the same session: the cutoff. A live bid is present, but the first
    # sell has no venue answer, and a second sell for the same lot is a short.
    cutoff = T0 + 5 * 3600 + 45 * 60
    intents = harness.engine.expiry_intents(cutoff, quotes={QQQ: 1.60})
    assert intents == []
    assert harness.engine.positions[QQQ].contracts == qty - 1
    pages = [r for r in reasons(harness, "alert") if "exit_in_flight" in r]
    assert pages, "a human must be told the remainder could not be flattened"
    assert len(harness.broker.placed) == 2


def test_a_transport_unknown_sell_restores_the_risk_and_never_retries(tmp_path):
    harness = build(tmp_path,
                    (True, None, ""),
                    TransportError("socket reset"))
    qty = enter(harness)

    send(harness, T0 + 60, CLOSE)
    assert harness.engine.positions[QQQ].contracts == qty
    assert harness.engine.exit_in_flight == {QQQ}
    assert len(harness.book.unknown_orders()) == 1
    assert list(harness.book.orders.values())[-1].state == PENDING
    assert "exit_unknown:transport" in reasons(harness, "alert")

    assert send(harness, T0 + 120, CLOSE) == []
    assert "close_while_exiting" in reasons(harness, "skip")
    assert len(harness.broker.placed) == 2


# ── buys: the two asymmetries ───────────────────────────────────────────────
def test_a_rejected_buy_frees_its_slot_and_a_later_entry_is_allowed(tmp_path):
    harness = build(tmp_path,
                    (False, 0, "insufficient_buying_power"),
                    (True, None, ""))
    intents = send(harness, T0, ENTRY)
    assert [i.action for i in intents] == ["buy"]
    assert harness.engine.open_positions == []
    assert harness.book.open_positions() == []
    assert list(harness.book.orders.values())[0].state == REJECTED

    intents = send(harness, T0 + 60, ENTRY)
    assert [i.action for i in intents] == ["buy"]
    assert harness.engine.open_positions[0].contracts == intents[0].qty
    assert len(harness.book.open_positions()) == 1


def test_an_unknown_buy_keeps_the_engine_position_because_the_exposure_may_be_real(tmp_path):
    harness = build(tmp_path, TransportError("socket reset"))
    qty = enter(harness)
    assert len(harness.broker.placed) == 1

    # The book confirms nothing, but the send may have arrived: the engine keeps the
    # position, so the next entry is refused as already-open instead of doubling it.
    assert harness.engine.positions[QQQ].contracts == qty
    assert harness.book.open_positions() == []
    assert len(harness.book.unknown_orders()) == 1
    assert send(harness, T0 + 60, ENTRY) == []
    assert "already_open" in reasons(harness, "skip")
    assert len(harness.broker.placed) == 1


def test_a_partial_buy_keeps_the_larger_position_until_the_venue_is_done(tmp_path):
    harness = build(tmp_path, (True, 1, ""))
    qty = enter(harness)
    assert qty >= 2
    # A partial is not terminal: the remainder can still fill, so shrinking the
    # engine's risk now would understate exposure.
    assert harness.engine.positions[QQQ].contracts == qty
    assert harness.book.open_positions()[0].contracts == 1
    assert list(harness.book.orders.values())[0].state == PARTIAL


def test_a_filled_sell_clears_every_in_flight_trace(tmp_path):
    harness = build(tmp_path, (True, None, ""), (True, None, ""))
    enter(harness)
    send(harness, T0 + 60, CLOSE)
    assert harness.engine.open_positions == []
    assert harness.engine.exit_in_flight == set()
    assert list(harness.book.orders.values())[-1].state == FILLED
    assert harness.book.realized_total > 0


# ── realised P&L follows the fill, not the post ─────────────────────────────
def test_a_fill_away_from_the_post_moves_realised_pnl_to_the_fill(tmp_path):
    """The post said 1.50; the venue filled 1.42. The governor must see 1.42."""
    harness = build(tmp_path, (True, None, ""), (True, None, "", 1.42))
    qty = enter(harness)
    send(harness, T0 + 60, CLOSE)

    confirmed = round((1.42 - 1.05) * qty * 100, 2)
    assert harness.book.realized_total == pytest.approx(confirmed)
    assert harness.engine.stats.realized_usd == pytest.approx(confirmed)
    assert harness.engine.governor_state.equity_usd == pytest.approx(confirmed)
    settles = [r for r in reasons(harness, "settle") if "confirmed_fill" in r]
    assert settles, "the correction must be auditable"


def test_a_rejected_sell_reverses_the_profit_the_engine_assumed(tmp_path):
    harness = build(tmp_path, (True, None, ""), (False, 0, "position_not_found"))
    qty = enter(harness)
    assumed = round((1.50 - 1.05) * qty * 100, 2)
    assert assumed > 0          # the engine books the post price at intent time

    send(harness, T0 + 60, CLOSE)
    assert harness.book.realized_total == 0.0
    assert harness.engine.stats.realized_usd == pytest.approx(0.0)
    assert harness.engine.governor_state.realized_today_usd == pytest.approx(0.0)
    assert harness.engine.positions[QQQ].contracts == qty


def test_a_partial_sell_books_only_the_contracts_that_traded(tmp_path):
    harness = build(tmp_path, (True, None, ""), (True, 1, ""))
    qty = enter(harness)
    send(harness, T0 + 60, CLOSE)

    assert harness.book.realized_total == pytest.approx((1.50 - 1.05) * 1 * 100)
    assert harness.engine.stats.realized_usd == pytest.approx((1.50 - 1.05) * 100)
    assert harness.engine.positions[QQQ].contracts == qty - 1


def test_the_correction_is_idempotent_under_a_redelivered_ack(tmp_path):
    harness = build(tmp_path, (True, None, ""), (True, None, "", 1.42))
    qty = enter(harness)
    send(harness, T0 + 60, CLOSE)
    corrected = harness.engine.stats.realized_usd
    order = list(harness.book.orders.values())[-1]

    # A redelivered cumulative answer is the same answer, not a second fill, and it
    # must not move the correction a second time.
    harness.listener._book_ack(
        order.intent, order.client_id,
        Ack(ok=True, contract_key=QQQ, qty=qty, price=1.42), T0 + 61)
    assert harness.engine.stats.realized_usd == pytest.approx(corrected)
    assert corrected == pytest.approx(round((1.42 - 1.05) * qty * 100, 2))

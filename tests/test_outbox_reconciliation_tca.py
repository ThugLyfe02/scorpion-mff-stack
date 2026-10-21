"""Outbox dispatch, reconciliation, and TCA on realised fills.

The tests that matter here are the ones about *not* doing something twice, *not* retrying a
refusal, and *not* letting a drifted book keep trading.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scorpion.authorization import OrderEnvelope
from scorpion.broker import Quote
from scorpion.config import Policy
from scorpion.domain import BookState, Effect, EffectKind, PositionState, PositionStatus
from scorpion.outbox import DispatchConfig, Dispatcher, Receipt
from scorpion.reconciliation import (
    BrokerPosition,
    reconcile,
    reconcile_and_halt,
)
from scorpion.store import Store
from scorpion.tca import Execution, analyse, execution_from_row, report_from_store

D = Decimal
NOW = datetime.now(UTC) + timedelta(hours=1)
KEY = "QQQ|CALL|719|2026-09-08"


def book() -> BookState:
    position = PositionState(
        KEY, status=PositionStatus.PENDING_ENTRY, generation=1, quantity=0,
        average_price=D("0"),
    )
    return BookState(positions={KEY: position})


def store(tmp_path) -> Store:
    s = Store(tmp_path / "scorpion.db")
    s.append_effects(
        [Effect(EffectKind.PROPOSE_OPEN, KEY, "e1", 1, "risk:full_size", quantity_hint=1)]
    )
    return s


class Sender:
    """A scripted venue. Records every envelope it is asked to send."""

    def __init__(self, *receipts: Receipt | Exception) -> None:
        self.receipts = list(receipts)
        self.seen: list[OrderEnvelope] = []

    def send(self, envelope: OrderEnvelope, *, now: datetime) -> Receipt:
        self.seen.append(envelope)
        item = self.receipts.pop(0) if len(self.receipts) > 1 else self.receipts[0]
        if isinstance(item, Exception):
            raise item
        return item


def dispatcher(tmp_path, sender, **cfg):
    return Dispatcher(
        store=store(tmp_path),
        sender=sender,
        policy=Policy(live_submission_enabled=True),
        config=DispatchConfig(**cfg),
        owner="test-worker",
    )


QUOTES = {KEY: Quote(bid=D("1.00"), ask=D("1.02"), observed_ts_utc=NOW)}


# ── dispatch: the happy path ────────────────────────────────────────────────
def test_a_due_effect_is_sent_once_and_the_execution_recorded(tmp_path):
    sender = Sender(Receipt(ok=True, broker_ref="ord-1", fill_price=D("1.02"), fill_qty=1))
    report = dispatcher(tmp_path, sender).dispatch_once(state=book(), now=NOW, quotes=QUOTES)
    assert report.attempted == 1 and report.sent == 1
    assert len(sender.seen) == 1
    assert sender.seen[0].idempotency_key == f"e1:{KEY}:1:PROPOSE_OPEN"
    rows = dispatcher(tmp_path, Sender(Receipt(ok=True))).store.load_executions()
    assert len(rows) == 1 and rows[0]["contract_key"] == KEY


def test_the_second_pass_finds_nothing_due(tmp_path):
    sender = Sender(Receipt(ok=True, broker_ref="ord-1"))
    d = dispatcher(tmp_path, sender)
    d.dispatch_once(state=book(), now=NOW, quotes=QUOTES)
    second = d.dispatch_once(state=book(), now=NOW, quotes=QUOTES)
    assert second.attempted == 0 and len(sender.seen) == 1


# ── dispatch: refusals are terminal ────────────────────────────────────────
def test_a_stale_quote_refuses_and_is_never_retried(tmp_path):
    stale = {KEY: Quote(bid=D("1.00"), ask=D("1.02"),
                       observed_ts_utc=NOW - timedelta(seconds=30))}
    sender = Sender(Receipt(ok=True))
    d = dispatcher(tmp_path, sender)
    report = d.dispatch_once(state=book(), now=NOW, quotes=stale)
    assert report.refused == 1 and sender.seen == []
    assert d.dispatch_once(state=book(), now=NOW, quotes=stale).attempted == 0
    assert d.store.outbox_snapshot()["counts"] == {"REFUSED": 1}


def test_a_non_order_effect_exits_the_queue_as_refused(tmp_path):
    s = Store(tmp_path / "s.db")
    s.append_effects([Effect(EffectKind.HALT, None, "e9", 0, "operator_stop")])
    report = Dispatcher(store=s, sender=Sender(Receipt(ok=True))).dispatch_once(
        state=book(), now=NOW
    )
    assert report.refused == 1 and report.sent == 0


def test_a_live_disabled_policy_refuses_without_calling_the_sender(tmp_path):
    sender = Sender(Receipt(ok=True))
    d = Dispatcher(store=store(tmp_path), sender=sender, policy=Policy())
    report = d.dispatch_once(state=book(), now=NOW, quotes=QUOTES)
    assert report.refused == 1 and sender.seen == []


# ── dispatch: retries, backoff, dead letters ───────────────────────────────
def test_a_retryable_failure_backs_off_and_stays_claimable(tmp_path):
    sender = Sender(Receipt(ok=False, error="timeout", retryable=True))
    d = dispatcher(tmp_path, sender, base_backoff_seconds=60)
    report = d.dispatch_once(state=book(), now=NOW, quotes=QUOTES)
    assert report.retried == 1
    assert d.store.outbox_snapshot()["counts"] == {"RETRY": 1}
    # not due yet
    assert d.dispatch_once(state=book(), now=NOW + timedelta(seconds=10), quotes=QUOTES).attempted == 0
    # due after the backoff
    assert d.dispatch_once(state=book(), now=NOW + timedelta(seconds=61), quotes=QUOTES).attempted == 1


def test_a_poison_effect_is_dead_lettered_after_max_attempts(tmp_path):
    sender = Sender(RuntimeError("venue down"))
    d = dispatcher(tmp_path, sender, max_attempts=2, base_backoff_seconds=0)
    d.dispatch_once(state=book(), now=NOW, quotes=QUOTES)
    second = d.dispatch_once(state=book(), now=NOW + timedelta(seconds=1), quotes=QUOTES)
    assert second.dead == 1
    snapshot = d.store.outbox_snapshot()
    assert snapshot["dead_letter"] == 1 and snapshot["counts"] == {"DEAD": 1}
    assert d.dispatch_once(state=book(), now=NOW + timedelta(seconds=2), quotes=QUOTES).attempted == 0


def test_backoff_doubles_and_is_capped():
    cfg = DispatchConfig(base_backoff_seconds=2, max_backoff_seconds=20)
    assert [cfg.backoff(n) for n in (1, 2, 3, 4, 5)] == [2, 4, 8, 16, 20]


def test_a_lease_stops_a_second_worker_claiming_the_same_effect(tmp_path):
    s = store(tmp_path)
    first = s.claim_due(owner="w1", now=NOW, limit=10, lease_seconds=30)
    second = s.claim_due(owner="w2", now=NOW, limit=10, lease_seconds=30)
    assert len(first) == 1 and second == []
    # after the lease expires the effect is claimable again, and the attempt was counted
    third = s.claim_due(owner="w2", now=NOW + timedelta(seconds=31), limit=10, lease_seconds=30)
    assert len(third) == 1 and int(str(third[0]["attempts"])) == 2


def test_the_execution_insert_is_idempotent_on_the_key(tmp_path):
    s = store(tmp_path)
    kwargs = dict(
        effect_id=1, idempotency_key="k", contract_key=KEY, side="BUY", quantity=1,
        limit_price="1.02", fill_price="1.02", fill_qty=1, intent_ts=NOW, fill_ts=NOW,
    )
    assert s.record_execution(**kwargs) is True
    assert s.record_execution(**kwargs) is False
    assert len(s.load_executions()) == 1


# ── reconciliation ─────────────────────────────────────────────────────────
def open_book(quantity: int = 10) -> BookState:
    position = PositionState(
        KEY, status=PositionStatus.OPEN, generation=1, quantity=quantity,
        average_price=D("1.00"),
    )
    return BookState(positions={KEY: position})


def test_matching_books_reconcile_clean():
    result = reconcile(open_book(10), [BrokerPosition(KEY, 10)])
    assert result.clean and not result.requires_halt


def test_a_position_the_venue_does_not_have_is_a_phantom():
    result = reconcile(open_book(10), [])
    assert not result.clean and result.drifts[0].kind == "phantom_local"
    assert result.drifts[0].book_quantity == 10 and result.drifts[0].broker_quantity == 0


def test_a_position_we_do_not_know_about_is_unhedged_risk():
    result = reconcile(BookState(), [BrokerPosition(KEY, 4)])
    assert result.drifts[0].kind == "missing_local" and result.requires_halt


def test_a_quantity_difference_is_a_mismatch():
    result = reconcile(open_book(10), [BrokerPosition(KEY, 6)])
    assert result.drifts[0].kind == "quantity_mismatch"


def test_tolerance_is_per_contract_and_explicit():
    tolerant = reconcile(open_book(10), [BrokerPosition(KEY, 8)], tolerance={KEY: 2})
    assert tolerant.clean
    strict = reconcile(open_book(10), [BrokerPosition(KEY, 8)])
    assert not strict.clean
    other = reconcile(open_book(10), [BrokerPosition(KEY, 8)], tolerance={"OTHER": 5})
    assert not other.clean


def test_a_negative_broker_quantity_is_rejected():
    with pytest.raises(ValueError):
        reconcile(open_book(1), [BrokerPosition(KEY, -1)])


def test_drift_halts_the_book_and_emits_a_halt_effect():
    state, effects, result = reconcile_and_halt(open_book(10), [])
    assert state.halted and len(effects) == 1
    assert effects[0].kind is EffectKind.HALT
    assert effects[0].reason.startswith("reconciliation_drift")
    assert result.requires_halt


def test_a_clean_reconcile_does_not_halt():
    state, effects, _ = reconcile_and_halt(open_book(5), [BrokerPosition(KEY, 5)])
    assert not state.halted and effects == ()


# ── TCA ────────────────────────────────────────────────────────────────────
def execution(**kw) -> Execution:
    base = dict(
        contract_key=KEY, side="BUY", quantity=10, limit_price=D("1.00"),
        fill_price=D("1.03"), fill_qty=10, intent_ts=NOW, fill_ts=NOW + timedelta(milliseconds=400),
        reference=None,
    )
    base.update(kw)
    return Execution(**base)


def test_a_buy_above_its_limit_costs_money():
    e = execution()
    assert e.cost_per_contract == D("0.03")
    assert e.cost_bps == pytest.approx(300.0)
    assert e.cost_usd == D("30.00")


def test_a_sell_below_its_limit_costs_money():
    e = execution(side="SELL", limit_price=D("2.00"), fill_price=D("1.95"))
    assert e.cost_per_contract == D("0.05") and e.cost_usd == D("50.00")


def test_a_better_fill_is_a_negative_cost():
    assert execution(fill_price=D("0.98")).cost_per_contract < 0


def test_partials_and_latency_are_visible():
    e = execution(fill_qty=4, fill_ts=NOW + timedelta(seconds=2))
    assert e.fill_ratio == pytest.approx(0.4)
    assert e.latency_ms == pytest.approx(2000.0)
    report = analyse([e])
    assert report.partial_share == 100.0 and report.latency_p90_ms == pytest.approx(2000.0)


def test_the_report_aggregates_and_suggests_nothing_from_too_few_fills():
    report = analyse([execution() for _ in range(5)])
    assert report.mean_cost_bps == pytest.approx(300.0)
    assert report.total_cost_usd == D("150.00")
    assert report.suggested_multiple() is None


def test_the_suggested_multiple_needs_references_and_rounds_up():
    fills = [
        execution(fill_price=D("1.051"), reference=D("1.00")) for _ in range(10)
    ]
    suggested = analyse(fills).suggested_multiple()
    assert suggested is not None and suggested >= 1.051


def test_executions_round_trip_through_the_store(tmp_path):
    s = store(tmp_path)
    s.record_execution(
        effect_id=1, idempotency_key="k", contract_key=KEY, side="BUY", quantity=5,
        limit_price="1.00", fill_price="1.02", fill_qty=5, intent_ts=NOW,
        fill_ts=NOW + timedelta(milliseconds=250),
    )
    rows = s.load_executions()
    report = report_from_store(rows)
    assert report.n == 1
    parsed = execution_from_row(rows[0])
    assert parsed.fill_price == D("1.02") and parsed.latency_ms == pytest.approx(250.0)
    assert report.mean_cost_bps == pytest.approx(200.0)

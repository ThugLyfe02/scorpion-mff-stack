import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.domain import BookState
from scorpion.ops_queue import QueuePriority, load_operator_inbox
from scorpion.parser import parse_message
from scorpion.pipeline import Pipeline
from scorpion.reconciliation import ExternalPositionObservation, reconcile_positions
from scorpion.reducer import apply_fill, reduce_book
from scorpion.resilience import OperationalMode, ResilienceAssessment
from scorpion.store import Store


def test_operator_inbox_prioritizes_blocked_system_packet(tmp_path, raw_factory):
    store = Store(tmp_path / "queue.db")
    pipeline = Pipeline(
        store,
        allowed_author_ids=frozenset({"author"}),
        resilience_assessment=ResilienceAssessment(OperationalMode.HALTED, (), ()),
    )
    asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))
    inbox = load_operator_inbox(store.path)
    assert len(inbox) == 1
    assert inbox[0].priority is QueuePriority.P0
    assert inbox[0].disposition == "BLOCKED_SYSTEM"


def test_operator_inbox_marks_old_actionable_packet_stale(tmp_path, raw_factory):
    store = Store(tmp_path / "stale.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.01")))
    inbox = load_operator_inbox(
        store.path,
        now=datetime.now(UTC) + timedelta(seconds=40),
    )
    assert inbox[0].stale is True
    assert inbox[0].priority <= QueuePriority.P1


def test_read_only_reconciliation_detects_quantity_mismatch(raw_factory):
    event = parse_message(raw_factory("QQQ 719C TODAY @ 1.01"))
    state, _ = reduce_book(BookState(), event)
    assert event.contract_key is not None
    state = apply_fill(
        state,
        event.contract_key,
        generation=1,
        quantity_delta=2,
        fill_price=Decimal("1.01"),
    )
    clean = reconcile_positions(
        state,
        (ExternalPositionObservation(event.contract_key, 2, Decimal("1.01")),),
    )
    assert clean.clean is True

    mismatch = reconcile_positions(
        state,
        (ExternalPositionObservation(event.contract_key, 1, Decimal("1.01")),),
    )
    assert mismatch.critical is True
    assert any(finding.code == "quantity_mismatch" for finding in mismatch.findings)


def test_read_only_reconciliation_detects_unexpected_external_position():
    report = reconcile_positions(
        BookState(),
        (ExternalPositionObservation("NVDA|PUT|227.5|2026-09-11", 3),),
    )
    assert report.critical is True
    assert report.findings[0].code == "unexpected_external_position"

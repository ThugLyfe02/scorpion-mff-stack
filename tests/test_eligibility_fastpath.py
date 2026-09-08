import asyncio
from datetime import timedelta
from decimal import Decimal

from scorpion.accuracy import AssociationEvidence
from scorpion.broker import ExecutionMode
from scorpion.decision_packet import (
    DecisionDisposition,
    build_decision_packet,
)
from scorpion.eligibility import (
    EligibilityDisposition,
    StrategyBucket,
    classify_eligibility,
)
from scorpion.fastpath import FastPathPreparer, FastPathStatus, QuoteCache
from scorpion.ops_queue import QueuePriority, load_operator_inbox
from scorpion.parser import parse_message, parse_message_with_evidence
from scorpion.pipeline import Pipeline
from scorpion.reducer import reduce_book
from scorpion.resilience import OperationalMode, ResilienceAssessment
from scorpion.sequence_guard import SequenceAssessment
from scorpion.store import Store
from scorpion.domain import BookState


def test_etf_and_lotto_are_research_only_but_core_single_name_is_allowed(raw_factory):
    qqq = parse_message(raw_factory("QQQ 719C TODAY @ 1.01"))
    qqq_decision = classify_eligibility(qqq)
    assert qqq_decision.bucket is StrategyBucket.ETF
    assert qqq_decision.disposition is EligibilityDisposition.RESEARCH_ONLY

    lotto = parse_message(raw_factory("TSLA 350C TODAY @ 1.20 ER LOTTO"))
    lotto_decision = classify_eligibility(lotto)
    assert lotto_decision.bucket is StrategyBucket.LOTTO
    assert lotto_decision.disposition is EligibilityDisposition.RESEARCH_ONLY

    aapl = parse_message(raw_factory("AAPL 200C TODAY @ 1.00"))
    core = classify_eligibility(aapl)
    assert core.bucket is StrategyBucket.CORE_SINGLE_NAME
    assert core.disposition is EligibilityDisposition.ALLOW_REVIEW


def test_pipeline_preserves_etf_signal_but_blocks_execution_progression(tmp_path, raw_factory):
    store = Store(tmp_path / "strategy-block.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    event, effects = asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))
    assert len(effects) == 1
    assert event.contract_key in pipeline.state.positions

    with store.connect() as db:
        effect = db.execute(
            "SELECT status FROM proposed_effects WHERE source_event_id=?",
            (event.event_id,),
        ).fetchone()
        packet = db.execute(
            "SELECT disposition,payload_json FROM operator_decision_packets WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
    assert effect["status"] == "BLOCKED_STRATEGY"
    assert packet["disposition"] == DecisionDisposition.BLOCKED_STRATEGY.value

    inbox = load_operator_inbox(
        store.path,
        now=event.received_ts_utc + timedelta(hours=1),
    )
    assert len(inbox) == 1
    assert inbox[0].priority is QueuePriority.P3
    assert inbox[0].stale is False


def _ready_core_packet(raw_factory):
    raw = raw_factory("AAPL 200C TODAY @ 1.00")
    parsed = parse_message_with_evidence(raw, frozenset({"author"}))
    event = parsed.event
    state, effects = reduce_book(BookState(), event)
    assert state.positions[event.contract_key].generation == 1
    packet = build_decision_packet(
        event,
        parsed.evidence,
        AssociationEvidence("not_followup", 1.0, 0),
        ResilienceAssessment(OperationalMode.NORMAL, (), ()),
        SequenceAssessment(()),
    )
    assert packet.disposition is DecisionDisposition.READY_FOR_OPERATOR_REVIEW
    return event, effects[0], packet


def test_fastpath_prepares_review_instruction_without_llm_or_live_submission(raw_factory):
    event, effect, packet = _ready_core_packet(raw_factory)
    now = event.source_ts_utc + timedelta(milliseconds=200)
    cache = QuoteCache()
    cache.update(
        event.contract_key,
        bid=Decimal("1.08"),
        ask=Decimal("1.10"),
        observed_ts_utc=now,
    )
    preparer = FastPathPreparer(cache, mode=ExecutionMode.REVIEW_ONLY)
    intent = preparer.prepare(event, effect, packet, quantity=1, now=now)
    assert intent.status is FastPathStatus.AWAITING_HUMAN_AUTHORIZATION
    assert intent.limit_price == Decimal("1.10")
    assert intent.preparation_latency_us >= 0

    result = preparer.dispatch(intent)
    assert result.status == "AWAITING_HUMAN_AUTHORIZATION"
    assert result.fill_price is None


def test_fastpath_waits_at_markup_cap_and_rejects_large_dislocation(raw_factory):
    event, effect, packet = _ready_core_packet(raw_factory)
    now = event.source_ts_utc + timedelta(milliseconds=200)
    cache = QuoteCache()
    preparer = FastPathPreparer(cache, mode=ExecutionMode.REVIEW_ONLY)

    cache.update(
        event.contract_key,
        bid=Decimal("1.18"),
        ask=Decimal("1.20"),
        observed_ts_utc=now,
    )
    waiting = preparer.prepare(event, effect, packet, quantity=1, now=now)
    assert waiting.status is FastPathStatus.WAITING_FOR_LIMIT
    assert waiting.limit_price == Decimal("1.1500")

    cache.update(
        event.contract_key,
        bid=Decimal("1.28"),
        ask=Decimal("1.30"),
        observed_ts_utc=now,
    )
    stale = preparer.prepare(event, effect, packet, quantity=1, now=now)
    assert stale.status is FastPathStatus.ENTRY_DISLOCATION

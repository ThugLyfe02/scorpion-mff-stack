from datetime import UTC, datetime
from decimal import Decimal

from scorpion.association import associate_followup_with_evidence
from scorpion.broker import ExecutionMode
from scorpion.decision_packet import DecisionDisposition, build_decision_packet
from scorpion.domain import BookState
from scorpion.fastpath import FastPathPreparer, FastPathStatus, QuoteCache
from scorpion.parser import parse_message_with_evidence
from scorpion.reducer import reduce_book
from scorpion.resilience import OperationalMode, ResilienceAssessment
from scorpion.sequence_guard import assess_sequence


def _event_effect_packet(raw):
    parsed = parse_message_with_evidence(raw, frozenset({"author"}))
    associated = associate_followup_with_evidence(parsed.event, BookState())
    event = associated.event
    state, effects = reduce_book(BookState(), event)
    assert state.positions
    packet = build_decision_packet(
        event,
        parsed.evidence,
        associated.evidence,
        ResilienceAssessment(OperationalMode.NORMAL, (), ()),
        assess_sequence(event, BookState()),
    )
    return event, effects[0], packet


def test_review_fastpath_prepares_without_agent_or_live_submission(raw_factory):
    event, effect, packet = _event_effect_packet(raw_factory("TSLA 345C TODAY @ 1.00"))
    assert packet.disposition is DecisionDisposition.READY_FOR_OPERATOR_REVIEW
    cache = QuoteCache()
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    cache.update(
        event.contract_key or "",
        bid=Decimal("1.00"),
        ask=Decimal("1.05"),
        observed_ts_utc=now,
    )
    preparer = FastPathPreparer(cache, mode=ExecutionMode.REVIEW_ONLY)
    intent = preparer.prepare(event, effect, packet, quantity=1, now=now)
    assert intent.status is FastPathStatus.AWAITING_HUMAN_AUTHORIZATION
    assert intent.limit_price == Decimal("1.05")
    assert intent.preparation_latency_us >= 0
    result = preparer.dispatch(intent)
    assert result.status == "AWAITING_HUMAN_AUTHORIZATION"


def test_paper_fastpath_can_fill_without_live_broker(raw_factory):
    event, effect, packet = _event_effect_packet(raw_factory("TSLA 345C TODAY @ 1.00"))
    cache = QuoteCache()
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    cache.update(
        event.contract_key or "",
        bid=Decimal("1.00"),
        ask=Decimal("1.05"),
        observed_ts_utc=now,
    )
    preparer = FastPathPreparer(cache, mode=ExecutionMode.PAPER)
    intent = preparer.prepare(event, effect, packet, quantity=1, now=now)
    assert intent.status is FastPathStatus.PAPER_READY
    result = preparer.dispatch(intent)
    assert result.status == "PAPER_FILLED"
    assert result.fill_price == Decimal("1.05")


def test_fastpath_blocks_etf_strategy_before_quote_use(raw_factory):
    event, effect, packet = _event_effect_packet(
        raw_factory(
            "QQQ 719C TODAY @ 1.01",
            channel_id="1448448931116748993",
        )
    )
    assert packet.disposition is DecisionDisposition.BLOCKED_STRATEGY
    intent = FastPathPreparer(QuoteCache()).prepare(
        event,
        effect,
        packet,
        quantity=1,
        now=datetime(2026, 9, 8, 14, 0, tzinfo=UTC),
    )
    assert intent.status is FastPathStatus.BLOCKED_STRATEGY


def test_fastpath_rejects_25_percent_entry_dislocation(raw_factory):
    event, effect, packet = _event_effect_packet(raw_factory("TSLA 345C TODAY @ 1.00"))
    cache = QuoteCache()
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    cache.update(
        event.contract_key or "",
        bid=Decimal("1.25"),
        ask=Decimal("1.30"),
        observed_ts_utc=now,
    )
    intent = FastPathPreparer(cache).prepare(
        event,
        effect,
        packet,
        quantity=1,
        now=now,
    )
    assert intent.status is FastPathStatus.ENTRY_DISLOCATION

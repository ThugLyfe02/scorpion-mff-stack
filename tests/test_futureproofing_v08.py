import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from scorpion.active_learning import ReviewCandidate
from scorpion.calibration import CalibrationStatus
from scorpion.confidence_sequence import (
    OnlineBernoulliMonitor,
    anytime_bernoulli_confidence_sequence,
)
from scorpion.discord_snowflake import (
    assess_snowflake_timestamp,
    decode_snowflake_timestamp,
)
from scorpion.domain import EventKind
from scorpion.inference_router import (
    InferenceContext,
    InferenceDepth,
    allocate_inference_budget,
    route_inference,
)
from scorpion.pipeline import Pipeline
from scorpion.quote_consensus import ConsensusQuoteCache, QuoteConsensusStatus
from scorpion.resilience import OperationalMode
from scorpion.review_allocator import ReviewWorkItem, allocate_review_budget
from scorpion.runtime import _queue_capacity
from scorpion.state_checkpoint import (
    create_state_checkpoint,
    load_latest_verified_checkpoint,
    restore_state,
)
from scorpion.store import Store
from scorpion.temporal_guard import assess_message_time

_DISCORD_EPOCH_MS = 1420070400000


def _snowflake(ts: datetime, low_bits: int = 1) -> str:
    milliseconds = int(ts.astimezone(UTC).timestamp() * 1000)
    return str(((milliseconds - _DISCORD_EPOCH_MS) << 22) | low_bits)


def _review_candidate(event_id: str, confidence: float) -> ReviewCandidate:
    return ReviewCandidate(
        event_id=event_id,
        parser_confidence=confidence,
        association_confidence=0.95,
        novelty_score=0.50,
        ensemble_entropy=0.20,
    )


def _inference_context(
    event_id: str,
    *,
    kind: EventKind = EventKind.ENTRY,
    parser_confidence: float = 0.99,
    association_confidence: float = 0.99,
    novelty_score: float = 0.05,
    ensemble_entropy: float = 0.02,
    calibration_status: CalibrationStatus = CalibrationStatus.TRUSTED,
    mode: OperationalMode = OperationalMode.NORMAL,
    parser_conflict: bool = False,
) -> InferenceContext:
    return InferenceContext(
        event_id=event_id,
        event_kind=kind,
        parser_confidence=parser_confidence,
        association_confidence=association_confidence,
        novelty_score=novelty_score,
        ensemble_entropy=ensemble_entropy,
        calibration_status=calibration_status,
        operational_mode=mode,
        parser_conflict=parser_conflict,
    )


def test_discord_snowflake_is_independent_causal_clock():
    source = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    message_id = _snowflake(source)
    decoded = decode_snowflake_timestamp(message_id)
    assert decoded is not None
    assert abs((decoded - source).total_seconds()) < 0.001
    assessment = assess_snowflake_timestamp(message_id, source)
    assert assessment.available is True
    assert assessment.consistent is True


def test_temporal_guard_quarantines_snowflake_api_clock_disagreement(raw_factory):
    encoded = datetime(2026, 9, 8, 13, 59, 50, tzinfo=UTC)
    raw = raw_factory("AAPL 200C TODAY @ 1.00", message_id=_snowflake(encoded))
    assessment = assess_message_time(raw)
    assert assessment.critical is True
    assert any(item.code == "discord_snowflake_clock_mismatch" for item in assessment.findings)


def test_quote_consensus_uses_robust_median_across_fresh_providers():
    cache = ConsensusQuoteCache(minimum_providers=3, maximum_midpoint_dispersion=0.03)
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    contract = "AAPL|CALL|200|2026-09-08"
    cache.update("a", contract, bid=Decimal("1.00"), ask=Decimal("1.04"), observed_ts_utc=now)
    cache.update("b", contract, bid=Decimal("1.01"), ask=Decimal("1.05"), observed_ts_utc=now)
    cache.update("c", contract, bid=Decimal("0.99"), ask=Decimal("1.03"), observed_ts_utc=now)
    consensus = cache.assess(contract, now=now)
    assert consensus.status is QuoteConsensusStatus.CONSENSUS
    assert consensus.quote is not None
    assert consensus.quote.bid == Decimal("1.0")
    assert consensus.quote.ask == Decimal("1.04")
    assert consensus.providers_used == ("a", "b", "c")


def test_quote_consensus_fails_closed_on_provider_disagreement():
    cache = ConsensusQuoteCache(minimum_providers=2, maximum_midpoint_dispersion=0.02)
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    contract = "AAPL|CALL|200|2026-09-08"
    cache.update("a", contract, bid=Decimal("1.00"), ask=Decimal("1.04"), observed_ts_utc=now)
    cache.update("b", contract, bid=Decimal("1.30"), ask=Decimal("1.34"), observed_ts_utc=now)
    consensus = cache.assess(contract, now=now)
    assert consensus.status is QuoteConsensusStatus.PROVIDER_DISAGREEMENT
    assert cache.get(contract, now=now) is None


def test_anytime_confidence_sequence_is_safe_for_repeated_monitoring():
    early = anytime_bernoulli_confidence_sequence(20, 20)
    later = anytime_bernoulli_confidence_sequence(2000, 2000)
    assert 0.0 <= early.lower_bound <= early.empirical_rate <= early.upper_bound <= 1.0
    assert later.lower_bound > early.lower_bound
    monitor = OnlineBernoulliMonitor()
    for _ in range(200):
        monitor.update(True)
    assert monitor.current().samples == 200
    assert monitor.current().lower_bound < 1.0


def test_review_allocator_spends_budget_on_marginal_information_not_duplicates():
    items = (
        ReviewWorkItem(_review_candidate("a", 0.40), "rule-x", "AAPL|CALL", "channel-1"),
        ReviewWorkItem(_review_candidate("b", 0.41), "rule-x", "AAPL|CALL", "channel-1"),
        ReviewWorkItem(_review_candidate("c", 0.55), "rule-y", "NVDA|PUT", "channel-2"),
    )
    allocation = allocate_review_budget(items, budget=2)
    selected = {item.event_id for item in allocation.selected}
    assert "a" in selected
    assert "c" in selected
    assert "b" in allocation.deferred_event_ids


def test_inference_router_keeps_obvious_calibrated_cases_off_expensive_models():
    route = route_inference(_inference_context("easy"))
    assert route.depth is InferenceDepth.NONE
    assert route.cost_units == 0
    assert "deterministic_evidence_sufficient" in route.reasons


def test_inference_router_escalates_novel_ambiguous_conflicting_cases():
    route = route_inference(
        _inference_context(
            "hard",
            kind=EventKind.AMBIGUOUS,
            parser_confidence=0.40,
            association_confidence=0.50,
            novelty_score=0.90,
            ensemble_entropy=0.60,
            calibration_status=CalibrationStatus.DEGRADED,
            mode=OperationalMode.DEGRADED,
            parser_conflict=True,
        )
    )
    assert route.depth is InferenceDepth.DEEP
    assert route.cost_units == 5
    assert "parser_conflict" in route.reasons


def test_inference_budget_spends_compute_on_highest_information_value():
    contexts = (
        _inference_context("easy"),
        _inference_context(
            "critical",
            kind=EventKind.AMBIGUOUS,
            parser_confidence=0.20,
            association_confidence=0.40,
            novelty_score=0.95,
            ensemble_entropy=0.80,
            calibration_status=CalibrationStatus.UNCALIBRATED,
            parser_conflict=True,
        ),
        _inference_context(
            "medium",
            parser_confidence=0.75,
            association_confidence=0.80,
            novelty_score=0.45,
            ensemble_entropy=0.20,
            calibration_status=CalibrationStatus.PROVISIONAL,
        ),
    )
    allocation = allocate_inference_budget(contexts, budget_units=5)
    selected = {route.event_id for route in allocation.selected}
    assert "critical" in selected
    assert "easy" in selected
    assert allocation.used_units == 5
    assert any(route.event_id == "medium" for route in allocation.deferred)


def test_verified_checkpoint_restores_only_tail_events(tmp_path, raw_factory):
    store = Store(tmp_path / "checkpoint.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00", message_id="first")))
    checkpoint = create_state_checkpoint(store.path, runtime_policy=pipeline.runtime_policy)
    assert checkpoint.signal_count == 1

    asyncio.run(
        pipeline.handle(
            raw_factory("NVDA 200P Sep 11 @ 1.00", message_id="second", minute=1)
        )
    )
    signals = store.load_signals()
    restored = restore_state(store.path, signals, runtime_policy=pipeline.runtime_policy)
    assert restored.used_checkpoint is True
    assert restored.tail_events == 1
    assert restored.state.seen_event_ids == pipeline.state.seen_event_ids


def test_checkpoint_policy_mismatch_falls_back_to_full_replay(tmp_path, raw_factory):
    store = Store(tmp_path / "checkpoint-policy.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00")))
    create_state_checkpoint(store.path, runtime_policy=pipeline.runtime_policy)
    changed = replace(pipeline.runtime_policy, policy_version="runtime-policy-v2")
    restored = restore_state(store.path, store.load_signals(), runtime_policy=changed)
    assert restored.used_checkpoint is False
    assert restored.reason == "full_replay_durable_process_order"


def test_corrupt_checkpoint_is_rejected_and_full_replay_remains_available(tmp_path, raw_factory):
    store = Store(tmp_path / "checkpoint-corrupt.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00")))
    checkpoint = create_state_checkpoint(store.path, runtime_policy=pipeline.runtime_policy)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE state_checkpoints SET state_json='{}' WHERE checkpoint_id=?",
            (checkpoint.checkpoint_id,),
        )
        db.commit()
    signals = store.load_signals()
    assert (
        load_latest_verified_checkpoint(
            store.path,
            signals,
            runtime_policy=pipeline.runtime_policy,
        )
        is None
    )
    restored = restore_state(store.path, signals, runtime_policy=pipeline.runtime_policy)
    assert restored.used_checkpoint is False


def test_persisted_event_processing_keeps_raw_receipt_separate(tmp_path, raw_factory):
    store = Store(tmp_path / "persisted.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    raw = raw_factory("AAPL 200C TODAY @ 1.00")
    assert store.append_raw(raw) is True
    event, effects = asyncio.run(pipeline.handle_persisted(raw))
    assert event.event_id in pipeline.state.seen_event_ids
    assert effects
    assert store.load_pending_raw() == []


def test_ingress_queue_capacity_is_fail_fast_on_bad_configuration(monkeypatch):
    monkeypatch.setenv("SCORPION_INGRESS_QUEUE_MAX", "1024")
    assert _queue_capacity() == 1024
    monkeypatch.setenv("SCORPION_INGRESS_QUEUE_MAX", "0")
    try:
        _queue_capacity()
    except ValueError as exc:
        assert "must be positive" in str(exc)
    else:
        raise AssertionError("zero ingress queue capacity should be rejected")

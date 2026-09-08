import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.broker import ExecutionMode
from scorpion.causal_trace import trace_event
from scorpion.certification import certify_runtime
from scorpion.delivery import DeliveryLedger, DeliveryState
from scorpion.domain import Effect, EffectKind, RawDiscordMessage
from scorpion.execution_forensics import ExecutionProfile
from scorpion.fastpath import FastPathStatus, PreparedExecutionIntent
from scorpion.governance import PromotionDecision, PromotionStatus
from scorpion.history_archive import ArchivedDiscordMessage, HistoryArchive
from scorpion.pipeline import Pipeline
from scorpion.quote_tape import HistoricalQuote, HistoricalQuoteTape
from scorpion.release_guard import ReleaseRegistry, ReleaseState
from scorpion.sizing_lab import SizingConstraints
from scorpion.store import Store
from scorpion.temporal_guard import (
    TemporalSeverity,
    assess_message_time,
    assess_quote_time,
    load_temporal_stream_report,
)
from scorpion.uncertainty_envelope import (
    ExecutionScenarioSpec,
    run_execution_uncertainty_envelope,
)

GUILD = "912747256736800838"
CHANNEL = "968352649437126676"
AUTHOR = "author"


def _intent(now: datetime) -> PreparedExecutionIntent:
    effect = Effect(
        EffectKind.PROPOSE_OPEN,
        "AAPL|CALL|200|2026-09-08",
        "event-1",
        1,
        "test",
    )
    return PreparedExecutionIntent(
        event_id="event-1",
        effect=effect,
        mode=ExecutionMode.REVIEW_ONLY,
        status=FastPathStatus.AWAITING_HUMAN_AUTHORIZATION,
        quantity=2,
        limit_price=Decimal("1.05"),
        quote=None,
        prepared_ts_utc=now,
        preparation_latency_us=100,
    )


def _archived(content: str, *, message_id: str, ts: datetime) -> ArchivedDiscordMessage:
    return ArchivedDiscordMessage(
        message_id=message_id,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=AUTHOR,
        source_ts_utc=ts,
        content=content,
    )


def test_delivery_ledger_is_idempotent_and_lease_safe(tmp_path):
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    ledger = DeliveryLedger(tmp_path / "delivery.db")
    first = ledger.register(_intent(now), policy_fingerprint="policy-a", now=now)
    second = ledger.register(_intent(now), policy_fingerprint="policy-a", now=now)
    assert first.delivery_id == second.delivery_id
    assert first.state is DeliveryState.PREPARED

    leased = ledger.acquire(first.delivery_id, owner="worker-a", now=now)
    assert leased is not None
    assert leased.state is DeliveryState.LEASED
    assert leased.attempt_count == 1
    assert ledger.acquire(first.delivery_id, owner="worker-b", now=now) is None

    terminal = ledger.acknowledge(
        first.delivery_id,
        owner="worker-a",
        accepted=True,
        now=now + timedelta(milliseconds=50),
    )
    assert terminal.state is DeliveryState.ACKNOWLEDGED
    assert ledger.acquire(
        first.delivery_id,
        owner="worker-c",
        now=now + timedelta(seconds=5),
    ) is None


def test_expired_delivery_lease_can_be_reacquired_without_new_instruction(tmp_path):
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    ledger = DeliveryLedger(tmp_path / "lease.db")
    record = ledger.register(_intent(now), policy_fingerprint="policy-a", now=now)
    assert ledger.acquire(
        record.delivery_id,
        owner="worker-a",
        lease=timedelta(milliseconds=100),
        now=now,
    ) is not None
    reacquired = ledger.acquire(
        record.delivery_id,
        owner="worker-b",
        now=now + timedelta(seconds=1),
    )
    assert reacquired is not None
    assert reacquired.delivery_id == record.delivery_id
    assert reacquired.attempt_count == 2


def test_release_guard_quarantines_degraded_release_without_auto_rollback(tmp_path):
    registry = ReleaseRegistry(tmp_path / "releases.db")
    ready = PromotionDecision(PromotionStatus.READY_FOR_OPERATOR_REVIEW, (), "ready")
    first = registry.register(
        component="parser",
        artifact_hash="artifact-a",
        policy_fingerprint="policy-a",
        research_manifest_hash="manifest-a",
    )
    registry.activate(first.release_id, operator="reviewer", promotion=ready)
    second = registry.register(
        component="parser",
        artifact_hash="artifact-b",
        policy_fingerprint="policy-b",
        research_manifest_hash="manifest-b",
        previous_release_id=first.release_id,
    )
    registry.activate(second.release_id, operator="reviewer", promotion=ready)

    plan = registry.quarantine_active("parser", reason="online drift sentinel fired")
    assert plan is not None
    assert plan.quarantined_release_id == second.release_id
    assert plan.rollback_release_id == first.release_id
    assert plan.requires_operator_activation is True
    assert registry.get(second.release_id).state is ReleaseState.QUARANTINED
    assert registry.get(first.release_id).state is ReleaseState.SUPERSEDED


def test_temporal_guard_catches_future_source_and_predecision_quote():
    received = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    raw = RawDiscordMessage(
        message_id="clock",
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=AUTHOR,
        content="AAPL 200C TODAY @ 1.00",
        source_ts_utc=received + timedelta(seconds=2),
        received_ts_utc=received,
    )
    assessment = assess_message_time(raw)
    assert assessment.critical is True
    assert assessment.findings[0].severity is TemporalSeverity.CRITICAL
    quote = assess_quote_time(
        decision_ts_utc=received,
        quote_ts_utc=received - timedelta(milliseconds=1),
    )
    assert quote.critical is True


def test_temporal_stream_report_uses_real_persisted_raw_clock_data(tmp_path):
    store = Store(tmp_path / "time.db")
    source = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    store.append_raw(
        RawDiscordMessage(
            message_id="1",
            guild_id=GUILD,
            channel_id=CHANNEL,
            author_id=AUTHOR,
            content="hello",
            source_ts_utc=source,
            received_ts_utc=source + timedelta(milliseconds=25),
        )
    )
    report = load_temporal_stream_report(store.path)
    assert report.messages == 1
    assert report.critical is False
    assert 20 <= report.receive_lag_p95_ms <= 30


def test_causal_trace_reconstructs_atomic_transition(tmp_path, raw_factory):
    store = Store(tmp_path / "trace.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    event, effects = asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00")))
    assert effects
    trace = trace_event(store.path, event.event_id)
    assert trace.complete is True
    assert trace.signal["event_id"] == event.event_id
    assert trace.raw is not None
    assert trace.decision_packet is not None
    assert trace.integrity is not None
    assert trace.stage_latency is not None
    assert len(trace.effects) == 1


def test_runtime_certification_proves_replay_and_evidence_invariance(tmp_path, raw_factory):
    store = Store(tmp_path / "certify.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00")))
    report = certify_runtime(store.path)
    assert report.passed is True
    assert {item.name for item in report.checks} >= {
        "schema_contract",
        "database_evidence_integrity",
        "duplicate_event_replay_invariance",
        "input_order_replay_invariance",
        "temporal_integrity",
    }


def test_execution_uncertainty_envelope_uses_worst_supported_scenario(tmp_path):
    archive = HistoryArchive(tmp_path / "history.db")
    opened = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    closed = opened + timedelta(minutes=1)
    archive.append(_archived("AAPL 200C TODAY @ 1.00", message_id="1", ts=opened))
    archive.append(_archived("closing runners", message_id="2", ts=closed))
    contract = "AAPL|CALL|200|2026-09-08"
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(
                contract,
                opened + timedelta(milliseconds=100),
                Decimal("1.00"),
                Decimal("1.02"),
                bid_size=10,
                ask_size=10,
            ),
            HistoricalQuote(
                contract,
                closed + timedelta(milliseconds=100),
                Decimal("1.20"),
                Decimal("1.22"),
                bid_size=10,
                ask_size=10,
            ),
        )
    )
    envelope = run_execution_uncertainty_envelope(
        archive,
        tape,
        channel_ids=frozenset({CHANNEL}),
        allowed_author_ids=frozenset({AUTHOR}),
        scenarios=(ExecutionScenarioSpec(50, 500, 1000),),
        constraints=SizingConstraints(
            min_samples=1,
            bootstrap_resamples=100,
            prior_strength=1.0,
        ),
        minimum_scenario_samples=1,
    )
    assert envelope.populated_scenarios == 1
    assert envelope.robust is True
    assert envelope.worst_conservative_edge > 0


def test_execution_uncertainty_envelope_fails_when_scenario_has_no_causal_quote(tmp_path):
    archive = HistoryArchive(tmp_path / "history-lag.db")
    opened = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    closed = opened + timedelta(minutes=1)
    archive.append(_archived("AAPL 200C TODAY @ 1.00", message_id="1", ts=opened))
    archive.append(_archived("closing runners", message_id="2", ts=closed))
    contract = "AAPL|CALL|200|2026-09-08"
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(
                contract,
                opened + timedelta(seconds=2),
                Decimal("1.00"),
                Decimal("1.02"),
                bid_size=10,
                ask_size=10,
            ),
            HistoricalQuote(
                contract,
                closed + timedelta(seconds=2),
                Decimal("1.20"),
                Decimal("1.22"),
                bid_size=10,
                ask_size=10,
            ),
        )
    )
    envelope = run_execution_uncertainty_envelope(
        archive,
        tape,
        channel_ids=frozenset({CHANNEL}),
        allowed_author_ids=frozenset({AUTHOR}),
        scenarios=(ExecutionScenarioSpec(50, 100, 100),),
        constraints=SizingConstraints(min_samples=1, bootstrap_resamples=100),
        minimum_scenario_samples=1,
    )
    assert envelope.robust is False
    assert "no_execution_scenario_has_sufficient_samples" in envelope.failures


def test_temporal_guard_handles_edited_timestamp_order(raw_factory):
    raw = raw_factory("AAPL 200C TODAY @ 1.00")
    broken = replace(raw, edited_ts_utc=raw.source_ts_utc - timedelta(seconds=1))
    assessment = assess_message_time(broken)
    assert any(item.code == "edit_precedes_create" for item in assessment.findings)

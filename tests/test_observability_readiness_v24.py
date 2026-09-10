import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scorpion.durable_ingress import append_raw_with_receipt
from scorpion.fail_safe_control import NoTradeSafetyLatch
from scorpion.hot_path_benchmark import HotPathBenchmarkPolicy, benchmark_hot_path
from scorpion.operator_observability import (
    OperatorSystemState,
    build_operator_observability_snapshot,
)
from scorpion.processing_order import inspect_processing_order
from scorpion.production_readiness import (
    ProductionReadinessPolicy,
    ProductionReadinessStatus,
    evaluate_production_readiness,
)
from scorpion.release_guard import ReleaseRegistry
from scorpion.store import Store

NOW = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
COMPONENT = "entry-quality-model"


def _raw(message_id: str = "r1"):
    from scorpion.domain import RawDiscordMessage

    return RawDiscordMessage(
        message_id=message_id,
        guild_id="benchmark-guild",
        channel_id="968352649437126676",
        author_id="author",
        content="QQQ 719C TODAY @ 1.01",
        source_ts_utc=NOW,
        received_ts_utc=NOW,
    )


def _promotion():
    from scorpion.governance import PromotionDecision, PromotionStatus

    return PromotionDecision(
        status=PromotionStatus.READY_FOR_OPERATOR_REVIEW,
        failures=(),
        reason="ready",
    )


def _seed_liveness(path: Path, *, status: str = "healthy", consumer_alive: bool = True) -> None:
    with sqlite3.connect(str(path)) as db:
        for component, metadata in (
            ("resilience-watchdog", {"status": "ok"}),
            (
                "discord-ingress-queue",
                {
                    "status": status,
                    "utilization": 0.10,
                    "consumer_alive": consumer_alive,
                },
            ),
        ):
            db.execute(
                """
                INSERT INTO heartbeats(component,last_seen_ts_utc,metadata_json)
                VALUES (?,?,?)
                ON CONFLICT(component) DO UPDATE SET
                    last_seen_ts_utc=excluded.last_seen_ts_utc,
                    metadata_json=excluded.metadata_json
                """,
                (component, NOW.isoformat(), json.dumps(metadata, sort_keys=True)),
            )


def _bootstrap_active_component(path: Path):
    Store(path)
    registry = ReleaseRegistry(path)
    release = registry.register(
        component=COMPONENT,
        artifact_hash="a" * 64,
        policy_fingerprint="p" * 64,
        research_manifest_hash="m" * 64,
        now=NOW - timedelta(minutes=2),
    )
    registry.activate(
        release.release_id,
        operator="operator-a",
        promotion=_promotion(),
        now=NOW - timedelta(minutes=1),
    )
    latch = NoTradeSafetyLatch(path)
    latch.clear_no_trade(
        COMPONENT,
        operator="operator-a",
        reason="initialize",
        source_release_id=release.release_id,
        now=NOW - timedelta(seconds=30),
    )
    _seed_liveness(path)
    return release


def _wide_benchmark_policy() -> HotPathBenchmarkPolicy:
    return HotPathBenchmarkPolicy(
        minimum_samples=8,
        maximum_core_p95_us=10_000_000,
        maximum_core_p99_us=10_000_000,
        maximum_receipt_p95_us=10_000_000,
        maximum_full_path_p95_us=10_000_000,
        maximum_full_path_p99_us=10_000_000,
        maximum_db_precommit_p95_us=10_000_000,
    )


def test_atomic_durable_ingress_binds_receipt_order_in_first_transaction(tmp_path):
    path = tmp_path / "atomic.db"
    Store(path)
    inserted = append_raw_with_receipt(path, _raw())
    assert inserted is True
    report = inspect_processing_order(path)
    assert report.raw_count == 1
    assert report.receipt_ordered_count == 1
    assert report.missing_raw_ids == ()


def test_hot_path_benchmark_separates_compute_receipt_and_full_path(tmp_path):
    report = benchmark_hot_path(
        samples=8,
        workspace=tmp_path / "benchmark",
        now=NOW,
        policy=_wide_benchmark_policy(),
    )
    assert report.passed is True
    assert report.atomic_receipt_order_verified is True
    assert report.core.p95_us >= 0
    assert report.durable_receipt.p95_us > 0
    assert report.full_path.p95_us >= report.durable_receipt.p50_us
    assert report.db_precommit_p95_us >= 0


def test_operator_observability_exposes_exact_release_safety_and_consumer_health(tmp_path):
    path = tmp_path / "operator.db"
    release = _bootstrap_active_component(path)

    healthy = build_operator_observability_snapshot(path, now=NOW)
    component = next(item for item in healthy.components if item.component == COMPONENT)
    assert component.active_release_id == release.release_id
    assert component.safety_source_release_id == release.release_id
    assert component.consistent is True
    assert healthy.system_state is OperatorSystemState.READY

    _seed_liveness(path, status="capture_only", consumer_alive=False)
    capture_only = build_operator_observability_snapshot(path, now=NOW)
    assert capture_only.system_state is OperatorSystemState.FAIL_CLOSED
    assert any("normalized_ingress_consumer_unhealthy" in item for item in capture_only.blockers)


def test_operator_observability_detects_release_safety_split_brain(tmp_path):
    path = tmp_path / "split.db"
    _bootstrap_active_component(path)
    with sqlite3.connect(str(path)) as db:
        db.execute(
            "UPDATE component_safety_state SET source_release_id='wrong-release' "
            "WHERE component=?",
            (COMPONENT,),
        )

    report = build_operator_observability_snapshot(path, now=NOW)
    assert report.system_state is OperatorSystemState.FAIL_CLOSED
    assert "RECONCILE_ACTIVE_RELEASE_SAFETY_BINDING" in report.next_actions


def test_production_readiness_gate_composes_observability_benchmark_and_chaos(tmp_path):
    path = tmp_path / "ready.db"
    release = _bootstrap_active_component(path)

    certificate = evaluate_production_readiness(
        path,
        workspace=tmp_path / "readiness",
        component=COMPONENT,
        operator="operator-readiness",
        now=NOW,
        policy=ProductionReadinessPolicy(
            benchmark_samples=8,
            require_chaos_drills=True,
        ),
        benchmark_policy=_wide_benchmark_policy(),
    )
    assert certificate.status is ProductionReadinessStatus.READY_FOR_OPERATOR_DECISION
    assert certificate.ready is True
    assert certificate.active_release_id == release.release_id
    assert certificate.hot_path_benchmark_id
    assert certificate.chaos_drill_id
    assert not certificate.failures


def test_production_readiness_gate_fails_closed_on_runtime_halt(tmp_path):
    path = tmp_path / "blocked.db"
    _bootstrap_active_component(path)
    Store(path).set_halt(True, "fault-injected")

    certificate = evaluate_production_readiness(
        path,
        workspace=tmp_path / "blocked-readiness",
        component=COMPONENT,
        operator="operator-readiness",
        now=NOW,
        policy=ProductionReadinessPolicy(
            benchmark_samples=8,
            require_chaos_drills=False,
        ),
        benchmark_policy=_wide_benchmark_policy(),
    )
    assert certificate.status is ProductionReadinessStatus.BLOCKED
    assert certificate.ready is False
    assert "operator_observability" in certificate.failures

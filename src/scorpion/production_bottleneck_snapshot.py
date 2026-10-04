from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import production_bottleneck_audit as _audit
from .production_bottleneck_audit import (
    BottleneckFinding,
    BottleneckSeverity,
    ProductionBottleneckAuditPolicy,
    ProductionBottleneckAuditReport,
    ProductionBottleneckSnapshot,
)


def audit_production_bottlenecks_connection(
    db: sqlite3.Connection,
    db_path: str | Path,
    *,
    now: datetime,
    policy: ProductionBottleneckAuditPolicy,
) -> ProductionBottleneckAuditReport:
    """Run the production bottleneck audit entirely from an existing SQLite snapshot."""
    timestamp = now.astimezone(UTC)
    path = Path(db_path)
    db.row_factory = sqlite3.Row
    page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
    freelist_pages = int(db.execute("PRAGMA freelist_count").fetchone()[0])
    db_bytes = path.stat().st_size if path.exists() else 0
    wal_path = Path(f"{path}-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    freelist_ratio = freelist_pages / page_count if page_count else 0.0
    pending_raw, pending_raw_age = _audit._pending_raw(db, now=timestamp)
    pending_review = (
        _audit._scalar(
            db,
            "SELECT COUNT(*) FROM proposed_effects WHERE status='PENDING_REVIEW'",
        )
        if _audit._table_exists(db, "proposed_effects")
        else 0
    )
    pending_deliveries = (
        _audit._scalar(
            db,
            "SELECT COUNT(*) FROM execution_delivery_ledger WHERE state IN ('PREPARED','LEASED')",
        )
        if _audit._table_exists(db, "execution_delivery_ledger")
        else 0
    )
    expired_leases = (
        _audit._scalar(
            db,
            """
            SELECT COUNT(*) FROM execution_delivery_ledger
            WHERE state='LEASED' AND lease_until_utc IS NOT NULL AND lease_until_utc <= ?
            """,
            (timestamp.isoformat(),),
        )
        if _audit._table_exists(db, "execution_delivery_ledger")
        else 0
    )
    ingress_metadata = _audit._ingress_metadata(db)
    queue_utilization = _audit._queue_utilization(ingress_metadata)
    ingress_consumer_alive = _audit._consumer_alive(ingress_metadata)
    pipeline_p95 = _audit._pipeline_p95(db, limit=policy.sample_limit)
    db_precommit_p95 = _audit._latency_p95(
        db,
        column="db_precommit_us",
        limit=policy.sample_limit,
    )
    stale_heartbeats = _audit._stale_heartbeats(
        db,
        now=timestamp,
        required=policy.required_heartbeats,
        maximum_age_seconds=policy.maximum_heartbeat_age_seconds,
    )
    release_conflicts = _audit._duplicate_active_count(
        db,
        table="component_releases",
        state_column="state",
        active_states=("ACTIVE",),
    )
    shadow_conflicts = _audit._duplicate_active_count(
        db,
        table="shadow_model_releases",
        state_column="state",
        active_states=("ACTIVE_SHADOW",),
    )
    rollout_conflicts = _audit._duplicate_active_count(
        db,
        table="deployment_rollouts",
        state_column="state",
        active_states=(
            "PREPARED",
            "ACTIVE_GUARDED",
            "HALTED",
            "ROLLBACK_PENDING",
            "ROLLBACK_VERIFYING",
            "RECOVERED_GUARDED",
            "RESUME_PENDING",
        ),
    )
    safety_conflicts = _audit._safety_conflicts(db)
    expired_dossiers = _audit._expired_ready_dossiers(db, now=timestamp)

    snapshot = ProductionBottleneckSnapshot(
        db_bytes=db_bytes,
        wal_bytes=wal_bytes,
        page_count=page_count,
        freelist_ratio=freelist_ratio,
        pending_raw=pending_raw,
        oldest_pending_raw_age_seconds=pending_raw_age,
        pending_review_effects=pending_review,
        pending_deliveries=pending_deliveries,
        expired_delivery_leases=expired_leases,
        queue_utilization=queue_utilization,
        pipeline_p95_us=pipeline_p95,
        db_precommit_p95_us=db_precommit_p95,
        stale_required_heartbeats=stale_heartbeats,
        active_release_conflicts=release_conflicts,
        active_shadow_conflicts=shadow_conflicts,
        active_rollout_conflicts=rollout_conflicts,
        safety_state_conflicts=safety_conflicts,
        expired_ready_dossiers=expired_dossiers,
        ingress_consumer_alive=ingress_consumer_alive,
    )
    findings: list[BottleneckFinding] = []

    _audit._find(
        findings,
        condition=queue_utilization > policy.maximum_queue_utilization,
        code="ingress_queue_pressure",
        surface="ingress",
        severity=BottleneckSeverity.CRITICAL,
        observed=queue_utilization,
        threshold=policy.maximum_queue_utilization,
        detail="durable ingress queue pressure risks callback backpressure",
    )
    _audit._find(
        findings,
        condition=not ingress_consumer_alive,
        code="ingress_consumer_not_alive",
        surface="ingress",
        severity=BottleneckSeverity.CRITICAL,
        observed=0.0,
        threshold=1.0,
        detail="ingress is capture-only or consumer liveness metadata is absent",
    )
    _audit._find(
        findings,
        condition=pending_raw > policy.maximum_pending_raw,
        code="pending_raw_backlog",
        surface="ingress",
        severity=BottleneckSeverity.CRITICAL,
        observed=float(pending_raw),
        threshold=float(policy.maximum_pending_raw),
        detail="unprocessed durable Discord revisions exceed rollout-safe backlog",
    )
    _audit._find(
        findings,
        condition=pending_raw_age > policy.maximum_pending_raw_age_seconds,
        code="pending_raw_age",
        surface="ingress",
        severity=BottleneckSeverity.CRITICAL,
        observed=pending_raw_age,
        threshold=policy.maximum_pending_raw_age_seconds,
        detail="old durable receipt is still waiting for normalized processing",
    )
    _audit._find(
        findings,
        condition=pending_review > policy.maximum_pending_review_effects,
        code="operator_review_backlog",
        surface="review",
        severity=BottleneckSeverity.WARNING,
        observed=float(pending_review),
        threshold=float(policy.maximum_pending_review_effects),
        detail="review backlog can stale downstream execution evidence",
        blocks_rollout=False,
    )
    _audit._find(
        findings,
        condition=pending_deliveries > policy.maximum_pending_deliveries,
        code="delivery_backlog",
        surface="delivery",
        severity=BottleneckSeverity.CRITICAL,
        observed=float(pending_deliveries),
        threshold=float(policy.maximum_pending_deliveries),
        detail="prepared/leased delivery ledger backlog exceeds safe operating envelope",
    )
    _audit._find(
        findings,
        condition=expired_leases > 0,
        code="expired_delivery_leases",
        surface="delivery",
        severity=BottleneckSeverity.CRITICAL,
        observed=float(expired_leases),
        threshold=0.0,
        detail="delivery leases expired without terminal acknowledgement",
    )
    _audit._find(
        findings,
        condition=wal_bytes > policy.maximum_wal_bytes,
        code="wal_growth",
        surface="storage",
        severity=BottleneckSeverity.WARNING,
        observed=float(wal_bytes),
        threshold=float(policy.maximum_wal_bytes),
        detail="WAL growth can amplify checkpoint and recovery latency",
        blocks_rollout=False,
    )
    _audit._find(
        findings,
        condition=freelist_ratio > policy.maximum_freelist_ratio,
        code="sqlite_freelist_bloat",
        surface="storage",
        severity=BottleneckSeverity.WARNING,
        observed=freelist_ratio,
        threshold=policy.maximum_freelist_ratio,
        detail="SQLite freelist ratio suggests avoidable I/O amplification",
        blocks_rollout=False,
    )
    _audit._find(
        findings,
        condition=pipeline_p95 > policy.maximum_pipeline_p95_us,
        code="pipeline_latency_p95",
        surface="pipeline",
        severity=BottleneckSeverity.CRITICAL,
        observed=pipeline_p95,
        threshold=policy.maximum_pipeline_p95_us,
        detail="normalized transition p95 exceeds rollout-safe latency",
    )
    _audit._find(
        findings,
        condition=db_precommit_p95 > policy.maximum_db_precommit_p95_us,
        code="db_precommit_latency_p95",
        surface="sqlite-writer",
        severity=BottleneckSeverity.CRITICAL,
        observed=db_precommit_p95,
        threshold=policy.maximum_db_precommit_p95_us,
        detail="serialized SQLite writer latency exceeds rollout-safe threshold",
    )
    for component in stale_heartbeats:
        findings.append(
            BottleneckFinding(
                code=f"stale_heartbeat:{component}",
                surface="liveness",
                severity=BottleneckSeverity.CRITICAL,
                observed=math.inf,
                threshold=policy.maximum_heartbeat_age_seconds,
                blocks_rollout=True,
                detail="required control-plane heartbeat is missing, stale, or future-dated",
            )
        )
    for code, count, surface in (
        ("multiple_active_releases", release_conflicts, "release"),
        ("multiple_active_shadows", shadow_conflicts, "shadow"),
        ("multiple_active_rollouts", rollout_conflicts, "rollout"),
        ("active_release_safety_conflict", safety_conflicts, "safety"),
    ):
        _audit._find(
            findings,
            condition=count > 0,
            code=code,
            surface=surface,
            severity=BottleneckSeverity.CRITICAL,
            observed=float(count),
            threshold=0.0,
            detail="mutually exclusive production state has conflicting active rows",
        )
    _audit._find(
        findings,
        condition=expired_dossiers > 0,
        code="expired_ready_dossiers",
        surface="promotion",
        severity=BottleneckSeverity.WARNING,
        observed=float(expired_dossiers),
        threshold=0.0,
        detail="expired ready dossiers should be retired to reduce stale operator choices",
        blocks_rollout=False,
    )

    rank = {
        BottleneckSeverity.CRITICAL: 3,
        BottleneckSeverity.WARNING: 2,
        BottleneckSeverity.INFO: 1,
    }
    dominant = ""
    if findings:
        dominant = max(
            findings,
            key=lambda item: (rank[item.severity], abs(item.observed - item.threshold)),
        ).code
    ready = not any(item.blocks_rollout for item in findings)
    material = {
        "version": "production-bottleneck-audit-v3",
        "generated_ts_utc": timestamp.isoformat(),
        "snapshot": asdict(snapshot),
        "findings": [asdict(item) for item in findings],
        "dominant_bottleneck": dominant,
        "ready_for_rollout": ready,
    }
    report_hash = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return ProductionBottleneckAuditReport(
        generated_ts_utc=timestamp,
        snapshot=snapshot,
        findings=tuple(findings),
        dominant_bottleneck=dominant,
        ready_for_rollout=ready,
        report_hash=report_hash,
    )

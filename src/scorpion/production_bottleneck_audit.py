from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class BottleneckSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class ProductionBottleneckAuditPolicy:
    sample_limit: int = 500
    maximum_queue_utilization: float = 0.80
    maximum_pending_raw: int = 64
    maximum_pending_raw_age_seconds: float = 30.0
    maximum_pending_review_effects: int = 500
    maximum_pending_deliveries: int = 128
    maximum_wal_bytes: int = 128 * 1024 * 1024
    maximum_freelist_ratio: float = 0.25
    maximum_pipeline_p95_us: float = 50_000.0
    maximum_db_precommit_p95_us: float = 20_000.0
    maximum_heartbeat_age_seconds: float = 5.0
    required_heartbeats: tuple[str, ...] = (
        "resilience-watchdog",
        "discord-ingress-queue",
    )

    def __post_init__(self) -> None:
        if self.sample_limit <= 0:
            raise ValueError("sample_limit must be positive")
        if not 0 < self.maximum_queue_utilization <= 1:
            raise ValueError("maximum_queue_utilization must be in (0,1]")
        if self.maximum_pending_raw < 0 or self.maximum_pending_review_effects < 0:
            raise ValueError("pending thresholds cannot be negative")
        if self.maximum_pending_deliveries < 0:
            raise ValueError("maximum_pending_deliveries cannot be negative")
        if self.maximum_pending_raw_age_seconds <= 0:
            raise ValueError("maximum_pending_raw_age_seconds must be positive")
        if self.maximum_wal_bytes < 0:
            raise ValueError("maximum_wal_bytes cannot be negative")
        if not 0 <= self.maximum_freelist_ratio <= 1:
            raise ValueError("maximum_freelist_ratio must be in [0,1]")
        if self.maximum_pipeline_p95_us <= 0 or self.maximum_db_precommit_p95_us <= 0:
            raise ValueError("latency thresholds must be positive")
        if self.maximum_heartbeat_age_seconds <= 0:
            raise ValueError("maximum_heartbeat_age_seconds must be positive")
        if len(self.required_heartbeats) != len(set(self.required_heartbeats)):
            raise ValueError("required_heartbeats must be unique")


@dataclass(frozen=True, slots=True)
class BottleneckFinding:
    code: str
    surface: str
    severity: BottleneckSeverity
    observed: float
    threshold: float
    blocks_rollout: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ProductionBottleneckSnapshot:
    db_bytes: int
    wal_bytes: int
    page_count: int
    freelist_ratio: float
    pending_raw: int
    oldest_pending_raw_age_seconds: float
    pending_review_effects: int
    pending_deliveries: int
    expired_delivery_leases: int
    queue_utilization: float
    pipeline_p95_us: float
    db_precommit_p95_us: float
    stale_required_heartbeats: tuple[str, ...]
    active_release_conflicts: int
    active_shadow_conflicts: int
    active_rollout_conflicts: int
    safety_state_conflicts: int
    expired_ready_dossiers: int


@dataclass(frozen=True, slots=True)
class ProductionBottleneckAuditReport:
    generated_ts_utc: datetime
    snapshot: ProductionBottleneckSnapshot
    findings: tuple[BottleneckFinding, ...]
    dominant_bottleneck: str
    ready_for_rollout: bool
    report_hash: str


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _scalar(db: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> int:
    row = db.execute(query, params).fetchone()
    return int(row[0]) if row is not None else 0


def _pending_raw(db: sqlite3.Connection, *, now: datetime) -> tuple[int, float]:
    if not (_table_exists(db, "raw_processing") and _table_exists(db, "raw_discord_events")):
        return 0, 0.0
    row = db.execute(
        """
        SELECT COUNT(*), MIN(r.received_ts_utc)
        FROM raw_processing p
        JOIN raw_discord_events r ON r.raw_event_id=p.raw_event_id
        WHERE p.status='PENDING'
        """
    ).fetchone()
    if row is None:
        return 0, 0.0
    count = int(row[0])
    oldest = row[1]
    if count <= 0 or oldest is None:
        return count, 0.0
    oldest_ts = datetime.fromisoformat(str(oldest)).astimezone(UTC)
    return count, max(0.0, (now - oldest_ts).total_seconds())


def _queue_utilization(db: sqlite3.Connection) -> float:
    if not _table_exists(db, "heartbeats"):
        return 0.0
    row = db.execute(
        "SELECT metadata_json FROM heartbeats WHERE component='discord-ingress-queue'"
    ).fetchone()
    if row is None:
        return 0.0
    try:
        metadata = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return 1.0
    value = metadata.get("utilization", 0.0)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 1.0
    return max(0.0, float(value))


def _latency_p95(db: sqlite3.Connection, *, column: str, limit: int) -> float:
    if not _table_exists(db, "transition_stage_latency"):
        return 0.0
    if column != "db_precommit_us":
        raise ValueError("unsupported stage latency column")
    rows = db.execute(
        f"SELECT {column} FROM transition_stage_latency "
        "ORDER BY created_ts_utc DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return _quantile([float(row[0]) for row in rows], 0.95)


def _pipeline_p95(db: sqlite3.Connection, *, limit: int) -> float:
    if not _table_exists(db, "decision_audit"):
        return 0.0
    rows = db.execute(
        "SELECT pipeline_latency_us FROM decision_audit "
        "ORDER BY created_ts_utc DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return _quantile([float(row[0]) for row in rows], 0.95)


def _stale_heartbeats(
    db: sqlite3.Connection,
    *,
    now: datetime,
    required: tuple[str, ...],
    maximum_age_seconds: float,
) -> tuple[str, ...]:
    if not _table_exists(db, "heartbeats"):
        return tuple(required)
    stale: list[str] = []
    for component in required:
        row = db.execute(
            "SELECT last_seen_ts_utc FROM heartbeats WHERE component=?",
            (component,),
        ).fetchone()
        if row is None:
            stale.append(component)
            continue
        timestamp = datetime.fromisoformat(str(row[0])).astimezone(UTC)
        if timestamp > now or (now - timestamp).total_seconds() > maximum_age_seconds:
            stale.append(component)
    return tuple(stale)


def _duplicate_active_count(
    db: sqlite3.Connection,
    *,
    table: str,
    state_column: str,
    active_states: tuple[str, ...],
) -> int:
    if not _table_exists(db, table):
        return 0
    placeholders = ",".join("?" for _ in active_states)
    row = db.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT component
            FROM {table}
            WHERE {state_column} IN ({placeholders})
            GROUP BY component
            HAVING COUNT(*) > 1
        )
        """,
        active_states,
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _safety_conflicts(db: sqlite3.Connection) -> int:
    if not _table_exists(db, "component_releases"):
        return 0
    if not _table_exists(db, "component_safety_state"):
        return _scalar(db, "SELECT COUNT(*) FROM component_releases WHERE state='ACTIVE'")
    row = db.execute(
        """
        SELECT COUNT(*)
        FROM component_releases r
        LEFT JOIN component_safety_state s ON s.component=r.component
        WHERE r.state='ACTIVE' AND (s.component IS NULL OR s.mode<>'NORMAL')
        """
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _expired_ready_dossiers(db: sqlite3.Connection, *, now: datetime) -> int:
    if not _table_exists(db, "production_promotion_dossiers"):
        return 0
    row = db.execute(
        """
        SELECT COUNT(*)
        FROM production_promotion_dossiers
        WHERE status='READY_FOR_APPROVAL' AND expires_ts_utc < ?
        """,
        (now.isoformat(),),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _find(
    findings: list[BottleneckFinding],
    *,
    condition: bool,
    code: str,
    surface: str,
    severity: BottleneckSeverity,
    observed: float,
    threshold: float,
    detail: str,
    blocks_rollout: bool = True,
) -> None:
    if condition:
        findings.append(
            BottleneckFinding(
                code=code,
                surface=surface,
                severity=severity,
                observed=observed,
                threshold=threshold,
                blocks_rollout=blocks_rollout,
                detail=detail,
            )
        )


def audit_production_bottlenecks(
    path: str | Path,
    *,
    now: datetime | None = None,
    policy: ProductionBottleneckAuditPolicy | None = None,
) -> ProductionBottleneckAuditReport:
    """Audit live control-plane pressure and mutually exclusive production state."""
    policy = policy or ProductionBottleneckAuditPolicy()
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)

    with sqlite3.connect(str(db_path), timeout=1.0) as db:
        db.row_factory = sqlite3.Row
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        freelist_pages = int(db.execute("PRAGMA freelist_count").fetchone()[0])
        db_bytes = db_path.stat().st_size
        wal_path = Path(f"{db_path}-wal")
        wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        freelist_ratio = freelist_pages / page_count if page_count else 0.0
        pending_raw, pending_raw_age = _pending_raw(db, now=timestamp)
        pending_review = (
            _scalar(
                db,
                "SELECT COUNT(*) FROM proposed_effects WHERE status='PENDING_REVIEW'",
            )
            if _table_exists(db, "proposed_effects")
            else 0
        )
        pending_deliveries = (
            _scalar(
                db,
                """
                SELECT COUNT(*) FROM execution_delivery_ledger
                WHERE state IN ('PREPARED','LEASED')
                """,
            )
            if _table_exists(db, "execution_delivery_ledger")
            else 0
        )
        expired_leases = (
            _scalar(
                db,
                """
                SELECT COUNT(*) FROM execution_delivery_ledger
                WHERE state='LEASED'
                  AND lease_until_utc IS NOT NULL
                  AND lease_until_utc <= ?
                """,
                (timestamp.isoformat(),),
            )
            if _table_exists(db, "execution_delivery_ledger")
            else 0
        )
        queue_utilization = _queue_utilization(db)
        pipeline_p95 = _pipeline_p95(db, limit=policy.sample_limit)
        db_precommit_p95 = _latency_p95(
            db,
            column="db_precommit_us",
            limit=policy.sample_limit,
        )
        stale_heartbeats = _stale_heartbeats(
            db,
            now=timestamp,
            required=policy.required_heartbeats,
            maximum_age_seconds=policy.maximum_heartbeat_age_seconds,
        )
        release_conflicts = _duplicate_active_count(
            db,
            table="component_releases",
            state_column="state",
            active_states=("ACTIVE",),
        )
        shadow_conflicts = _duplicate_active_count(
            db,
            table="shadow_model_releases",
            state_column="state",
            active_states=("ACTIVE_SHADOW",),
        )
        rollout_conflicts = _duplicate_active_count(
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
            ),
        )
        safety_conflicts = _safety_conflicts(db)
        expired_dossiers = _expired_ready_dossiers(db, now=timestamp)

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
    )
    findings: list[BottleneckFinding] = []

    _find(
        findings,
        condition=queue_utilization > policy.maximum_queue_utilization,
        code="ingress_queue_pressure",
        surface="ingress",
        severity=BottleneckSeverity.CRITICAL,
        observed=queue_utilization,
        threshold=policy.maximum_queue_utilization,
        detail="durable ingress queue pressure risks callback backpressure",
    )
    _find(
        findings,
        condition=pending_raw > policy.maximum_pending_raw,
        code="pending_raw_backlog",
        surface="ingress",
        severity=BottleneckSeverity.CRITICAL,
        observed=float(pending_raw),
        threshold=float(policy.maximum_pending_raw),
        detail="unprocessed durable Discord revisions exceed rollout-safe backlog",
    )
    _find(
        findings,
        condition=pending_raw_age > policy.maximum_pending_raw_age_seconds,
        code="pending_raw_age",
        surface="ingress",
        severity=BottleneckSeverity.CRITICAL,
        observed=pending_raw_age,
        threshold=policy.maximum_pending_raw_age_seconds,
        detail="old durable receipt is still waiting for normalized processing",
    )
    _find(
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
    _find(
        findings,
        condition=pending_deliveries > policy.maximum_pending_deliveries,
        code="delivery_backlog",
        surface="delivery",
        severity=BottleneckSeverity.CRITICAL,
        observed=float(pending_deliveries),
        threshold=float(policy.maximum_pending_deliveries),
        detail="prepared/leased delivery ledger backlog exceeds safe operating envelope",
    )
    _find(
        findings,
        condition=expired_leases > 0,
        code="expired_delivery_leases",
        surface="delivery",
        severity=BottleneckSeverity.CRITICAL,
        observed=float(expired_leases),
        threshold=0.0,
        detail="delivery leases expired without terminal acknowledgement",
    )
    _find(
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
    _find(
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
    _find(
        findings,
        condition=pipeline_p95 > policy.maximum_pipeline_p95_us,
        code="pipeline_latency_p95",
        surface="pipeline",
        severity=BottleneckSeverity.CRITICAL,
        observed=pipeline_p95,
        threshold=policy.maximum_pipeline_p95_us,
        detail="normalized transition p95 exceeds rollout-safe latency",
    )
    _find(
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
        _find(
            findings,
            condition=count > 0,
            code=code,
            surface=surface,
            severity=BottleneckSeverity.CRITICAL,
            observed=float(count),
            threshold=0.0,
            detail="mutually exclusive production state has conflicting active rows",
        )
    _find(
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
        "version": "production-bottleneck-audit-v2",
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

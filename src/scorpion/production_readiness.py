from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .hot_path_benchmark import HotPathBenchmarkPolicy, HotPathBenchmarkReport, benchmark_hot_path
from .operator_observability import (
    OperatorObservabilitySnapshot,
    OperatorSystemState,
    build_operator_observability_snapshot,
)
from .production_bottleneck_audit import (
    ProductionBottleneckAuditPolicy,
    ProductionBottleneckAuditReport,
)
from .production_chaos_drills import (
    PartialOutageChaosReport,
    run_isolated_partial_outage_chaos_drills,
)
from .production_gate import (
    ProductionAuthorization,
    ProductionAuthorizationStatus,
    ProductionPromotionDossier,
)
from .promotion_evidence_schema import PromotionEvidenceValidationReport
from .schema_contract import SCHEMA_CONTRACT_VERSION, inspect_schema


class ProductionReadinessStatus(StrEnum):
    READY_FOR_OPERATOR_DECISION = "READY_FOR_OPERATOR_DECISION"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class ProductionReadinessPolicy:
    certificate_ttl: timedelta = timedelta(minutes=2)
    benchmark_samples: int = 80
    require_chaos_drills: bool = True
    require_active_release: bool = True

    def __post_init__(self) -> None:
        if self.certificate_ttl <= timedelta(0):
            raise ValueError("certificate_ttl must be positive")
        if self.benchmark_samples <= 0:
            raise ValueError("benchmark_samples must be positive")


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    code: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ControlStateBinding:
    active_release_ids: tuple[str, ...]
    safety_mode: str
    safety_source_release_id: str
    safety_updated_ts_utc: str
    safety_event_count: int
    safety_head_event_id: str
    safety_chain_hash: str
    runtime_halt_value: str
    runtime_halt_updated_ts_utc: str
    heartbeat_metadata: tuple[tuple[str, str], ...]
    pending_raw: int
    pending_deliveries: int
    rollout_rows: tuple[tuple[str, str, int], ...]
    state_hash: str


@dataclass(frozen=True, slots=True)
class ProductionReadinessCertificate:
    certificate_id: str
    component: str
    active_release_id: str
    generated_ts_utc: datetime
    expires_ts_utc: datetime
    schema_contract_version: str
    operator_snapshot_hash: str
    operator_state_hash: str
    bottleneck_report_hash: str
    bottleneck_state_hash: str
    hot_path_benchmark_id: str
    chaos_drill_id: str
    control_state_hash: str
    safety_event_count: int
    safety_head_event_id: str
    safety_chain_hash: str
    policy_hash: str
    status: ProductionReadinessStatus
    checks: tuple[ReadinessCheck, ...]
    failures: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.status is ProductionReadinessStatus.READY_FOR_OPERATOR_DECISION


@dataclass(frozen=True, slots=True)
class RolloutReadinessCertificate:
    certificate_id: str
    production_readiness_certificate_id: str
    production_readiness_policy_hash: str
    schema_contract_version: str
    component: str
    candidate_release_id: str
    expected_predecessor_release_id: str
    candidate_artifact_hash: str
    candidate_policy_fingerprint: str
    candidate_research_manifest_hash: str
    dossier_id: str
    dossier_evidence_hash: str
    dossier_policy_hash: str
    authorization_id: str
    authorization_expires_ts_utc: datetime
    evidence_bundle_hash: str
    evidence_validation_hash: str
    generated_ts_utc: datetime
    expires_ts_utc: datetime
    operator_snapshot_hash: str
    operator_state_hash: str
    bottleneck_report_hash: str
    bottleneck_state_hash: str
    hot_path_benchmark_id: str
    chaos_drill_id: str
    control_state_hash: str
    safety_event_count: int
    safety_head_event_id: str
    safety_chain_hash: str
    bottleneck_policy: ProductionBottleneckAuditPolicy
    status: ProductionReadinessStatus
    failures: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.status is ProductionReadinessStatus.READY_FOR_OPERATOR_DECISION


@dataclass(frozen=True, slots=True)
class RolloutReadinessVerification:
    valid: bool
    failures: tuple[str, ...]


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _check(code: str, passed: bool, detail: str) -> ReadinessCheck:
    return ReadinessCheck(code=code, passed=passed, detail=detail)


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def bottleneck_state_fingerprint(report: ProductionBottleneckAuditReport) -> str:
    """Hash bottleneck state without its observation timestamp or evidence-instance hash."""
    return _hash_payload(
        {
            "version": "production-bottleneck-state-v1",
            "snapshot": asdict(report.snapshot),
            "findings": [asdict(item) for item in report.findings],
            "dominant_bottleneck": report.dominant_bottleneck,
            "ready_for_rollout": report.ready_for_rollout,
        }
    )


def operator_state_fingerprint(snapshot: OperatorObservabilitySnapshot) -> str:
    """Hash operator-visible state independently from the snapshot generation timestamp."""
    return _hash_payload(
        {
            "version": "operator-production-state-v1",
            "system_state": snapshot.system_state.value,
            "schema_compatible": snapshot.schema_compatible,
            "runtime_halted": snapshot.runtime_halted,
            "halt_reason": snapshot.halt_reason,
            "bottleneck_state_hash": bottleneck_state_fingerprint(
                snapshot.bottleneck_report
            ),
            "latency_report": asdict(snapshot.latency_report),
            "components": [asdict(item) for item in snapshot.components],
            "pending_raw_revisions": snapshot.pending_raw_revisions,
            "pending_review_effects": snapshot.pending_review_effects,
            "pending_deliveries": snapshot.pending_deliveries,
            "expired_delivery_leases": snapshot.expired_delivery_leases,
            "stale_required_heartbeats": snapshot.stale_required_heartbeats,
            "blockers": snapshot.blockers,
            "warnings": snapshot.warnings,
            "next_actions": snapshot.next_actions,
        }
    )


def capture_control_state_binding(
    db: sqlite3.Connection,
    *,
    component: str,
    required_heartbeats: tuple[str, ...],
) -> ControlStateBinding:
    """Capture DB-critical rollout state for TOCTOU comparison inside a write transaction."""
    db.row_factory = sqlite3.Row
    active_release_ids: tuple[str, ...] = ()
    if _table_exists(db, "component_releases"):
        active_release_ids = tuple(
            sorted(
                str(row["release_id"])
                for row in db.execute(
                    "SELECT release_id FROM component_releases "
                    "WHERE component=? AND state='ACTIVE'",
                    (component,),
                ).fetchall()
            )
        )

    safety_mode = ""
    safety_source_release_id = ""
    safety_updated_ts_utc = ""
    if _table_exists(db, "component_safety_state"):
        row = db.execute(
            "SELECT mode,source_release_id,updated_ts_utc FROM component_safety_state "
            "WHERE component=?",
            (component,),
        ).fetchone()
        if row is not None:
            safety_mode = str(row["mode"])
            safety_source_release_id = str(row["source_release_id"])
            safety_updated_ts_utc = str(row["updated_ts_utc"])

    safety_event_count = 0
    safety_head_event_id = ""
    safety_chain_hash = ""
    if _table_exists(db, "component_safety_integrity_state"):
        row = db.execute(
            "SELECT event_count,head_event_id,head_chain_hash "
            "FROM component_safety_integrity_state WHERE component=?",
            (component,),
        ).fetchone()
        if row is not None:
            safety_event_count = int(row["event_count"])
            safety_head_event_id = str(row["head_event_id"])
            safety_chain_hash = str(row["head_chain_hash"])

    runtime_halt_value = ""
    runtime_halt_updated_ts_utc = ""
    if _table_exists(db, "runtime_flags"):
        row = db.execute(
            "SELECT value,updated_ts_utc FROM runtime_flags WHERE key='halt'"
        ).fetchone()
        if row is not None:
            runtime_halt_value = str(row["value"])
            runtime_halt_updated_ts_utc = str(row["updated_ts_utc"])

    heartbeat_metadata: list[tuple[str, str]] = []
    if _table_exists(db, "heartbeats"):
        for heartbeat in required_heartbeats:
            row = db.execute(
                "SELECT metadata_json FROM heartbeats WHERE component=?",
                (heartbeat,),
            ).fetchone()
            heartbeat_metadata.append(
                (
                    heartbeat,
                    str(row["metadata_json"]) if row is not None else "",
                )
            )
    else:
        heartbeat_metadata.extend((heartbeat, "") for heartbeat in required_heartbeats)

    pending_raw = 0
    if _table_exists(db, "raw_processing"):
        row = db.execute(
            "SELECT COUNT(*) AS n FROM raw_processing WHERE status='PENDING'"
        ).fetchone()
        pending_raw = int(row["n"]) if row is not None else 0

    pending_deliveries = 0
    if _table_exists(db, "execution_delivery_ledger"):
        row = db.execute(
            "SELECT COUNT(*) AS n FROM execution_delivery_ledger "
            "WHERE state IN ('PREPARED','LEASED')"
        ).fetchone()
        pending_deliveries = int(row["n"]) if row is not None else 0

    rollout_rows: tuple[tuple[str, str, int], ...] = ()
    if _table_exists(db, "deployment_rollouts"):
        rollout_rows = tuple(
            sorted(
                (
                    str(row["rollout_id"]),
                    str(row["state"]),
                    int(row["generation"]),
                )
                for row in db.execute(
                    "SELECT rollout_id,state,generation FROM deployment_rollouts "
                    "WHERE component=? AND state NOT IN ('SUPERSEDED','CANCELLED','EXPIRED')",
                    (component,),
                ).fetchall()
            )
        )

    material = {
        "version": "production-control-state-binding-v1",
        "component": component,
        "active_release_ids": active_release_ids,
        "safety_mode": safety_mode,
        "safety_source_release_id": safety_source_release_id,
        "safety_updated_ts_utc": safety_updated_ts_utc,
        "safety_event_count": safety_event_count,
        "safety_head_event_id": safety_head_event_id,
        "safety_chain_hash": safety_chain_hash,
        "runtime_halt_value": runtime_halt_value,
        "runtime_halt_updated_ts_utc": runtime_halt_updated_ts_utc,
        "heartbeat_metadata": heartbeat_metadata,
        "pending_raw": pending_raw,
        "pending_deliveries": pending_deliveries,
        "rollout_rows": rollout_rows,
    }
    return ControlStateBinding(
        active_release_ids=active_release_ids,
        safety_mode=safety_mode,
        safety_source_release_id=safety_source_release_id,
        safety_updated_ts_utc=safety_updated_ts_utc,
        safety_event_count=safety_event_count,
        safety_head_event_id=safety_head_event_id,
        safety_chain_hash=safety_chain_hash,
        runtime_halt_value=runtime_halt_value,
        runtime_halt_updated_ts_utc=runtime_halt_updated_ts_utc,
        heartbeat_metadata=tuple(heartbeat_metadata),
        pending_raw=pending_raw,
        pending_deliveries=pending_deliveries,
        rollout_rows=rollout_rows,
        state_hash=_hash_payload(material),
    )


def _read_control_state_binding(
    path: Path,
    *,
    component: str,
    required_heartbeats: tuple[str, ...],
) -> ControlStateBinding:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        return capture_control_state_binding(
            db,
            component=component,
            required_heartbeats=required_heartbeats,
        )


def _readiness_policy_hash(
    policy: ProductionReadinessPolicy,
    benchmark_policy: HotPathBenchmarkPolicy | None,
    bottleneck_policy: ProductionBottleneckAuditPolicy,
) -> str:
    return _hash_payload(
        {
            "version": "production-readiness-policy-v2",
            "readiness": asdict(policy),
            "benchmark": asdict(benchmark_policy) if benchmark_policy is not None else None,
            "bottleneck": asdict(bottleneck_policy),
        }
    )


def _production_certificate_material(
    certificate: ProductionReadinessCertificate,
) -> dict[str, object]:
    return {
        "version": "production-readiness-v2",
        "component": certificate.component,
        "active_release_id": certificate.active_release_id,
        "generated_ts_utc": certificate.generated_ts_utc.astimezone(UTC).isoformat(),
        "expires_ts_utc": certificate.expires_ts_utc.astimezone(UTC).isoformat(),
        "schema_contract_version": certificate.schema_contract_version,
        "operator_snapshot_hash": certificate.operator_snapshot_hash,
        "operator_state_hash": certificate.operator_state_hash,
        "bottleneck_report_hash": certificate.bottleneck_report_hash,
        "bottleneck_state_hash": certificate.bottleneck_state_hash,
        "hot_path_benchmark_id": certificate.hot_path_benchmark_id,
        "chaos_drill_id": certificate.chaos_drill_id,
        "control_state_hash": certificate.control_state_hash,
        "safety_event_count": certificate.safety_event_count,
        "safety_head_event_id": certificate.safety_head_event_id,
        "safety_chain_hash": certificate.safety_chain_hash,
        "policy_hash": certificate.policy_hash,
        "status": certificate.status.value,
        "checks": [asdict(check) for check in certificate.checks],
        "failures": certificate.failures,
    }


def verify_production_readiness_certificate(
    certificate: ProductionReadinessCertificate,
    *,
    now: datetime | None = None,
) -> bool:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    expected = _hash_payload(_production_certificate_material(certificate))
    return (
        expected == certificate.certificate_id
        and certificate.ready
        and certificate.generated_ts_utc.astimezone(UTC) <= timestamp
        and timestamp <= certificate.expires_ts_utc.astimezone(UTC)
    )


def evaluate_production_readiness(
    path: str | Path,
    *,
    workspace: str | Path,
    component: str,
    operator: str,
    now: datetime | None = None,
    policy: ProductionReadinessPolicy | None = None,
    benchmark_policy: HotPathBenchmarkPolicy | None = None,
    bottleneck_policy: ProductionBottleneckAuditPolicy | None = None,
) -> ProductionReadinessCertificate:
    """Produce a short-lived host/control-plane readiness certificate without changing prod state."""
    policy = policy or ProductionReadinessPolicy()
    effective_bottleneck_policy = bottleneck_policy or ProductionBottleneckAuditPolicy()
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    if not component.strip() or not operator.strip():
        raise ValueError("component and operator are required")
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)

    schema = inspect_schema(db_path)
    snapshot: OperatorObservabilitySnapshot = build_operator_observability_snapshot(
        db_path,
        now=timestamp,
        bottleneck_policy=effective_bottleneck_policy,
    )
    benchmark: HotPathBenchmarkReport = benchmark_hot_path(
        samples=policy.benchmark_samples,
        workspace=workspace_path / "hot-path",
        now=timestamp,
        policy=benchmark_policy,
    )
    chaos: PartialOutageChaosReport | None = None
    if policy.require_chaos_drills:
        chaos = run_isolated_partial_outage_chaos_drills(
            workspace_path / "chaos",
            component=component,
            operator=operator,
            now=timestamp,
        )

    component_state = next(
        (item for item in snapshot.components if item.component == component),
        None,
    )
    active_release_id = component_state.active_release_id if component_state is not None else ""
    control = _read_control_state_binding(
        db_path,
        component=component,
        required_heartbeats=effective_bottleneck_policy.required_heartbeats,
    )
    checks = [
        _check(
            "schema_contract",
            schema.compatible,
            "database satisfies the complete production schema contract",
        ),
        _check(
            "operator_observability",
            snapshot.system_state is OperatorSystemState.READY,
            "operator snapshot has no blocking or degraded production seams",
        ),
        _check(
            "production_bottlenecks",
            snapshot.bottleneck_report.ready_for_rollout,
            "live control-plane bottleneck report is rollout-safe",
        ),
        _check(
            "hot_path_benchmark",
            benchmark.passed,
            "isolated host benchmark satisfies core/receipt/full-path latency budgets",
        ),
        _check(
            "atomic_receipt_order",
            benchmark.atomic_receipt_order_verified,
            "raw durability and receipt sequencing are complete at the first write boundary",
        ),
        _check(
            "component_observed",
            component_state is not None,
            "requested production component exists in operator observability",
        ),
        _check(
            "component_consistency",
            component_state is not None and component_state.consistent,
            "active release, rollout and safety provenance agree",
        ),
        _check(
            "active_release",
            bool(active_release_id) or not policy.require_active_release,
            "component has one exact active release",
        ),
        _check(
            "control_state_active_release",
            control.active_release_ids == ((active_release_id,) if active_release_id else ()),
            "DB-critical control binding agrees with the operator-visible active release",
        ),
    ]
    if chaos is not None:
        checks.append(
            _check(
                "partial_outage_chaos",
                chaos.passed,
                "isolated outage drills all demonstrated fail-closed behavior",
            )
        )

    failures = tuple(check.code for check in checks if not check.passed)
    status = (
        ProductionReadinessStatus.READY_FOR_OPERATOR_DECISION
        if not failures
        else ProductionReadinessStatus.BLOCKED
    )
    expires = timestamp + policy.certificate_ttl
    policy_hash = _readiness_policy_hash(
        policy,
        benchmark_policy,
        effective_bottleneck_policy,
    )
    provisional = ProductionReadinessCertificate(
        certificate_id="",
        component=component,
        active_release_id=active_release_id,
        generated_ts_utc=timestamp,
        expires_ts_utc=expires,
        schema_contract_version=SCHEMA_CONTRACT_VERSION,
        operator_snapshot_hash=snapshot.snapshot_id,
        operator_state_hash=operator_state_fingerprint(snapshot),
        bottleneck_report_hash=snapshot.bottleneck_report.report_hash,
        bottleneck_state_hash=bottleneck_state_fingerprint(snapshot.bottleneck_report),
        hot_path_benchmark_id=benchmark.benchmark_id,
        chaos_drill_id=chaos.drill_id if chaos is not None else "",
        control_state_hash=control.state_hash,
        safety_event_count=control.safety_event_count,
        safety_head_event_id=control.safety_head_event_id,
        safety_chain_hash=control.safety_chain_hash,
        policy_hash=policy_hash,
        status=status,
        checks=tuple(checks),
        failures=failures,
    )
    return replace(
        provisional,
        certificate_id=_hash_payload(_production_certificate_material(provisional)),
    )


def _validation_fingerprint(report: PromotionEvidenceValidationReport) -> str:
    return _hash_payload(
        {
            "version": "promotion-evidence-validation-binding-v1",
            "valid": report.valid,
            "records": report.records,
            "required_kinds": report.required_kinds,
            "failures": report.failures,
        }
    )


def _rollout_certificate_material(
    certificate: RolloutReadinessCertificate,
) -> dict[str, object]:
    return {
        "version": "rollout-readiness-capability-v1",
        "production_readiness_certificate_id": (
            certificate.production_readiness_certificate_id
        ),
        "production_readiness_policy_hash": certificate.production_readiness_policy_hash,
        "schema_contract_version": certificate.schema_contract_version,
        "component": certificate.component,
        "candidate_release_id": certificate.candidate_release_id,
        "expected_predecessor_release_id": certificate.expected_predecessor_release_id,
        "candidate_artifact_hash": certificate.candidate_artifact_hash,
        "candidate_policy_fingerprint": certificate.candidate_policy_fingerprint,
        "candidate_research_manifest_hash": certificate.candidate_research_manifest_hash,
        "dossier_id": certificate.dossier_id,
        "dossier_evidence_hash": certificate.dossier_evidence_hash,
        "dossier_policy_hash": certificate.dossier_policy_hash,
        "authorization_id": certificate.authorization_id,
        "authorization_expires_ts_utc": (
            certificate.authorization_expires_ts_utc.astimezone(UTC).isoformat()
        ),
        "evidence_bundle_hash": certificate.evidence_bundle_hash,
        "evidence_validation_hash": certificate.evidence_validation_hash,
        "generated_ts_utc": certificate.generated_ts_utc.astimezone(UTC).isoformat(),
        "expires_ts_utc": certificate.expires_ts_utc.astimezone(UTC).isoformat(),
        "operator_snapshot_hash": certificate.operator_snapshot_hash,
        "operator_state_hash": certificate.operator_state_hash,
        "bottleneck_report_hash": certificate.bottleneck_report_hash,
        "bottleneck_state_hash": certificate.bottleneck_state_hash,
        "hot_path_benchmark_id": certificate.hot_path_benchmark_id,
        "chaos_drill_id": certificate.chaos_drill_id,
        "control_state_hash": certificate.control_state_hash,
        "safety_event_count": certificate.safety_event_count,
        "safety_head_event_id": certificate.safety_head_event_id,
        "safety_chain_hash": certificate.safety_chain_hash,
        "bottleneck_policy": asdict(certificate.bottleneck_policy),
        "status": certificate.status.value,
        "failures": certificate.failures,
    }


def issue_rollout_readiness_certificate(
    path: str | Path,
    *,
    workspace: str | Path,
    component: str,
    candidate_release_id: str,
    dossier: ProductionPromotionDossier,
    authorization: ProductionAuthorization,
    evidence_validation: PromotionEvidenceValidationReport,
    evidence_bundle_hash: str,
    operator: str,
    now: datetime | None = None,
    policy: ProductionReadinessPolicy | None = None,
    benchmark_policy: HotPathBenchmarkPolicy | None = None,
    bottleneck_policy: ProductionBottleneckAuditPolicy | None = None,
) -> RolloutReadinessCertificate:
    """Issue a short-lived, single-candidate rollout capability from current production truth."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    effective_bottleneck_policy = bottleneck_policy or ProductionBottleneckAuditPolicy()
    host = evaluate_production_readiness(
        path,
        workspace=workspace,
        component=component,
        operator=operator,
        now=timestamp,
        policy=policy,
        benchmark_policy=benchmark_policy,
        bottleneck_policy=effective_bottleneck_policy,
    )
    db_path = Path(path)
    failures: list[str] = []
    if not host.ready:
        failures.append("production_readiness_blocked")
    if not dossier.ready_for_approval:
        failures.append("production_dossier_not_ready")
    if dossier.component != component:
        failures.append("dossier_component_mismatch")
    if authorization.status is not ProductionAuthorizationStatus.AUTHORIZED_FOR_OPERATOR_ACTIVATION:
        failures.append("production_authorization_not_active")
    if authorization.dossier_id != dossier.dossier_id:
        failures.append("authorization_dossier_mismatch")
    if authorization.authorized_until_ts_utc is None:
        failures.append("authorization_expiry_missing")
    if not evidence_validation.valid:
        failures.append("promotion_evidence_invalid")
    if not candidate_release_id.strip() or not evidence_bundle_hash.strip():
        failures.append("candidate_or_evidence_binding_missing")

    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    candidate_artifact = ""
    candidate_policy = ""
    candidate_manifest = ""
    predecessor = ""
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        candidate = db.execute(
            "SELECT * FROM component_releases WHERE release_id=?",
            (candidate_release_id,),
        ).fetchone()
        if candidate is None:
            failures.append("candidate_release_missing")
        else:
            candidate_artifact = str(candidate["artifact_hash"])
            candidate_policy = str(candidate["policy_fingerprint"])
            candidate_manifest = str(candidate["research_manifest_hash"])
            predecessor = str(candidate["previous_release_id"] or "")
            if str(candidate["component"]) != component:
                failures.append("candidate_component_mismatch")
            if str(candidate["state"]) not in {"CANDIDATE", "SUPERSEDED"}:
                failures.append("candidate_state_not_rollout_eligible")
            if candidate_artifact != dossier.artifact_sha256:
                failures.append("candidate_artifact_dossier_mismatch")
            if predecessor != dossier.parent_release_id:
                failures.append("candidate_predecessor_dossier_mismatch")

        control = capture_control_state_binding(
            db,
            component=component,
            required_heartbeats=effective_bottleneck_policy.required_heartbeats,
        )
    expected_active = (predecessor,) if predecessor else ()
    if control.active_release_ids != expected_active:
        failures.append("active_predecessor_changed_before_readiness_issue")
    if host.active_release_id != predecessor:
        failures.append("host_readiness_predecessor_mismatch")
    if control.safety_mode != "NORMAL":
        failures.append("safety_latch_not_normal")
    if predecessor and control.safety_source_release_id != predecessor:
        failures.append("safety_predecessor_binding_mismatch")
    if control.safety_event_count <= 0 or not control.safety_head_event_id:
        failures.append("safety_generation_missing")

    authorization_expiry = (
        authorization.authorized_until_ts_utc.astimezone(UTC)
        if authorization.authorized_until_ts_utc is not None
        else timestamp
    )
    expires = min(host.expires_ts_utc, dossier.expires_ts_utc, authorization_expiry)
    if timestamp > expires:
        failures.append("rollout_readiness_expired_at_issue")
    status = (
        ProductionReadinessStatus.READY_FOR_OPERATOR_DECISION
        if not failures
        else ProductionReadinessStatus.BLOCKED
    )
    provisional = RolloutReadinessCertificate(
        certificate_id="",
        production_readiness_certificate_id=host.certificate_id,
        production_readiness_policy_hash=host.policy_hash,
        schema_contract_version=host.schema_contract_version,
        component=component,
        candidate_release_id=candidate_release_id,
        expected_predecessor_release_id=predecessor,
        candidate_artifact_hash=candidate_artifact,
        candidate_policy_fingerprint=candidate_policy,
        candidate_research_manifest_hash=candidate_manifest,
        dossier_id=dossier.dossier_id,
        dossier_evidence_hash=dossier.evidence_hash,
        dossier_policy_hash=dossier.policy_hash,
        authorization_id=authorization.authorization_id,
        authorization_expires_ts_utc=authorization_expiry,
        evidence_bundle_hash=evidence_bundle_hash,
        evidence_validation_hash=_validation_fingerprint(evidence_validation),
        generated_ts_utc=timestamp,
        expires_ts_utc=expires,
        operator_snapshot_hash=host.operator_snapshot_hash,
        operator_state_hash=host.operator_state_hash,
        bottleneck_report_hash=host.bottleneck_report_hash,
        bottleneck_state_hash=host.bottleneck_state_hash,
        hot_path_benchmark_id=host.hot_path_benchmark_id,
        chaos_drill_id=host.chaos_drill_id,
        control_state_hash=control.state_hash,
        safety_event_count=control.safety_event_count,
        safety_head_event_id=control.safety_head_event_id,
        safety_chain_hash=control.safety_chain_hash,
        bottleneck_policy=effective_bottleneck_policy,
        status=status,
        failures=tuple(failures),
    )
    return replace(
        provisional,
        certificate_id=_hash_payload(_rollout_certificate_material(provisional)),
    )


def verify_rollout_readiness_certificate(
    certificate: RolloutReadinessCertificate,
    *,
    candidate_release_id: str,
    dossier: ProductionPromotionDossier,
    authorization: ProductionAuthorization,
    evidence_validation: PromotionEvidenceValidationReport,
    evidence_bundle_hash: str,
    now: datetime | None = None,
) -> RolloutReadinessVerification:
    """Verify immutable rollout bindings before any production-state transaction begins."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    failures: list[str] = []
    if _hash_payload(_rollout_certificate_material(certificate)) != certificate.certificate_id:
        failures.append("readiness_certificate_integrity_mismatch")
    if not certificate.ready:
        failures.append("readiness_certificate_blocked")
    generated = certificate.generated_ts_utc.astimezone(UTC)
    expires = certificate.expires_ts_utc.astimezone(UTC)
    if generated > timestamp:
        failures.append("readiness_certificate_future_dated")
    if timestamp > expires:
        failures.append("readiness_certificate_expired")
    if certificate.schema_contract_version != SCHEMA_CONTRACT_VERSION:
        failures.append("readiness_schema_contract_changed")
    if certificate.candidate_release_id != candidate_release_id:
        failures.append("readiness_candidate_mismatch")
    if certificate.component != dossier.component:
        failures.append("readiness_component_mismatch")
    if certificate.dossier_id != dossier.dossier_id:
        failures.append("readiness_dossier_mismatch")
    if certificate.dossier_evidence_hash != dossier.evidence_hash:
        failures.append("readiness_dossier_evidence_mismatch")
    if certificate.dossier_policy_hash != dossier.policy_hash:
        failures.append("readiness_dossier_policy_mismatch")
    if certificate.candidate_artifact_hash != dossier.artifact_sha256:
        failures.append("readiness_candidate_artifact_mismatch")
    if certificate.expected_predecessor_release_id != dossier.parent_release_id:
        failures.append("readiness_predecessor_mismatch")
    if certificate.authorization_id != authorization.authorization_id:
        failures.append("readiness_authorization_mismatch")
    if authorization.dossier_id != dossier.dossier_id:
        failures.append("authorization_dossier_mismatch")
    if authorization.authorized_until_ts_utc is None:
        failures.append("authorization_expiry_missing")
    else:
        current_expiry = authorization.authorized_until_ts_utc.astimezone(UTC)
        if current_expiry != certificate.authorization_expires_ts_utc.astimezone(UTC):
            failures.append("readiness_authorization_expiry_mismatch")
        if timestamp > current_expiry:
            failures.append("production_authorization_expired")
    if authorization.status is not ProductionAuthorizationStatus.AUTHORIZED_FOR_OPERATOR_ACTIVATION:
        failures.append("production_authorization_not_active")
    if certificate.evidence_bundle_hash != evidence_bundle_hash:
        failures.append("readiness_evidence_bundle_mismatch")
    if certificate.evidence_validation_hash != _validation_fingerprint(evidence_validation):
        failures.append("readiness_evidence_validation_mismatch")
    if not evidence_validation.valid:
        failures.append("promotion_evidence_invalid")
    return RolloutReadinessVerification(valid=not failures, failures=tuple(failures))

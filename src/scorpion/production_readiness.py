from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .hot_path_benchmark import HotPathBenchmarkPolicy, HotPathBenchmarkReport, benchmark_hot_path
from .operator_observability import (
    OperatorObservabilitySnapshot,
    OperatorSystemState,
    build_operator_observability_snapshot,
)
from .production_bottleneck_audit import ProductionBottleneckAuditPolicy
from .production_chaos_drills import (
    PartialOutageChaosReport,
    run_isolated_partial_outage_chaos_drills,
)
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
class ProductionReadinessCertificate:
    certificate_id: str
    component: str
    active_release_id: str
    generated_ts_utc: datetime
    expires_ts_utc: datetime
    schema_contract_version: str
    operator_snapshot_hash: str
    bottleneck_report_hash: str
    hot_path_benchmark_id: str
    chaos_drill_id: str
    status: ProductionReadinessStatus
    checks: tuple[ReadinessCheck, ...]
    failures: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.status is ProductionReadinessStatus.READY_FOR_OPERATOR_DECISION


def _check(code: str, passed: bool, detail: str) -> ReadinessCheck:
    return ReadinessCheck(code=code, passed=passed, detail=detail)


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
        bottleneck_policy=bottleneck_policy,
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
    material = {
        "version": "production-readiness-v1",
        "component": component,
        "active_release_id": active_release_id,
        "generated_ts_utc": timestamp.isoformat(),
        "expires_ts_utc": expires.isoformat(),
        "schema_contract_version": SCHEMA_CONTRACT_VERSION,
        "operator_snapshot_hash": snapshot.snapshot_id,
        "bottleneck_report_hash": snapshot.bottleneck_report.report_hash,
        "hot_path_benchmark_id": benchmark.benchmark_id,
        "chaos_drill_id": chaos.drill_id if chaos is not None else "",
        "status": status.value,
        "checks": [asdict(check) for check in checks],
        "failures": failures,
    }
    identifier = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return ProductionReadinessCertificate(
        certificate_id=identifier,
        component=component,
        active_release_id=active_release_id,
        generated_ts_utc=timestamp,
        expires_ts_utc=expires,
        schema_contract_version=SCHEMA_CONTRACT_VERSION,
        operator_snapshot_hash=snapshot.snapshot_id,
        bottleneck_report_hash=snapshot.bottleneck_report.report_hash,
        hot_path_benchmark_id=benchmark.benchmark_id,
        chaos_drill_id=chaos.drill_id if chaos is not None else "",
        status=status,
        checks=tuple(checks),
        failures=failures,
    )

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .auto_trainer import TrainingRunReport
from .canary import CanaryReport, CanaryStatus
from .drift_retraining import ChallengerRetrainingPlan
from .fail_safe_control import SafetyLatchState, SafetyMode
from .governance import PromotionDecision, PromotionStatus
from .liquidity_capacity import LiquidityCapacityReport
from .shadow_lifecycle import ShadowReleaseRecord, ShadowReleaseState


class ProductionGateStatus(StrEnum):
    BLOCKED = "BLOCKED"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"


class ProductionAuthorizationStatus(StrEnum):
    BLOCKED = "BLOCKED"
    PENDING_APPROVALS = "PENDING_APPROVALS"
    AUTHORIZED_FOR_OPERATOR_ACTIVATION = "AUTHORIZED_FOR_OPERATOR_ACTIVATION"
    EXPIRED = "EXPIRED"


class ProductionApprovalRole(StrEnum):
    MODEL_REVIEWER = "MODEL_REVIEWER"
    RISK_REVIEWER = "RISK_REVIEWER"


@dataclass(frozen=True, slots=True)
class ProductionPromotionPolicy:
    minimum_shadow_soak: timedelta = timedelta(minutes=30)
    maximum_training_age: timedelta = timedelta(days=14)
    maximum_live_evidence_age: timedelta = timedelta(minutes=15)
    minimum_canary_pairs: int = 100
    minimum_capacity_clip_multiplier: float = 1.0
    dossier_ttl: timedelta = timedelta(minutes=30)
    required_roles: tuple[ProductionApprovalRole, ...] = (
        ProductionApprovalRole.MODEL_REVIEWER,
        ProductionApprovalRole.RISK_REVIEWER,
    )
    require_distinct_operators: bool = True

    def __post_init__(self) -> None:
        if self.minimum_shadow_soak < timedelta(0):
            raise ValueError("minimum_shadow_soak cannot be negative")
        if self.maximum_training_age <= timedelta(0):
            raise ValueError("maximum_training_age must be positive")
        if self.maximum_live_evidence_age <= timedelta(0):
            raise ValueError("maximum_live_evidence_age must be positive")
        if self.minimum_canary_pairs <= 0:
            raise ValueError("minimum_canary_pairs must be positive")
        if self.minimum_capacity_clip_multiplier <= 0:
            raise ValueError("minimum_capacity_clip_multiplier must be positive")
        if self.dossier_ttl <= timedelta(0):
            raise ValueError("dossier_ttl must be positive")
        if not self.required_roles:
            raise ValueError("at least one production approval role is required")
        if len(self.required_roles) != len(set(self.required_roles)):
            raise ValueError("production approval roles must be unique")


@dataclass(frozen=True, slots=True)
class ProductionGateEvidence:
    component: str
    retraining_plan: ChallengerRetrainingPlan
    training: TrainingRunReport
    shadow: ShadowReleaseRecord
    promotion: PromotionDecision
    canary: CanaryReport
    capacity: LiquidityCapacityReport
    safety_state: SafetyLatchState
    runtime_certified: bool
    drift_active: bool
    policy_fingerprint: str
    research_manifest_hash: str
    canary_observed_ts_utc: datetime
    capacity_observed_ts_utc: datetime
    runtime_certified_ts_utc: datetime
    observed_ts_utc: datetime

    def __post_init__(self) -> None:
        if not self.component.strip():
            raise ValueError("component is required")
        if not self.policy_fingerprint.strip() or not self.research_manifest_hash.strip():
            raise ValueError("policy_fingerprint and research_manifest_hash are required")
        for name in (
            "canary_observed_ts_utc",
            "capacity_observed_ts_utc",
            "runtime_certified_ts_utc",
            "observed_ts_utc",
        ):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ProductionPromotionDossier:
    dossier_id: str
    component: str
    training_run_id: str
    shadow_release_id: str
    artifact_sha256: str
    parent_release_id: str
    evidence_hash: str
    policy_hash: str
    created_ts_utc: datetime
    expires_ts_utc: datetime
    status: ProductionGateStatus
    failures: tuple[str, ...]

    @property
    def ready_for_approval(self) -> bool:
        return self.status is ProductionGateStatus.READY_FOR_APPROVAL


@dataclass(frozen=True, slots=True)
class ProductionApproval:
    approval_id: str
    dossier_id: str
    role: ProductionApprovalRole
    operator: str
    created_ts_utc: datetime


@dataclass(frozen=True, slots=True)
class ProductionAuthorization:
    authorization_id: str
    dossier_id: str
    status: ProductionAuthorizationStatus
    approvals: tuple[ProductionApproval, ...]
    authorized_until_ts_utc: datetime | None
    failures: tuple[str, ...]

    @property
    def authorized(self) -> bool:
        return self.status is ProductionAuthorizationStatus.AUTHORIZED_FOR_OPERATOR_ACTIVATION


_SCHEMA = """
CREATE TABLE IF NOT EXISTS production_promotion_dossiers (
    dossier_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_ts_utc TEXT NOT NULL,
    dossier_json TEXT NOT NULL,
    dossier_sha256 TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS production_promotion_approvals (
    approval_id TEXT PRIMARY KEY,
    dossier_id TEXT NOT NULL,
    role TEXT NOT NULL,
    operator TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    FOREIGN KEY(dossier_id) REFERENCES production_promotion_dossiers(dossier_id),
    UNIQUE(dossier_id, role),
    UNIQUE(dossier_id, operator)
);
CREATE INDEX IF NOT EXISTS idx_production_dossier_component_created
ON production_promotion_dossiers(component,created_ts_utc,dossier_id);
CREATE INDEX IF NOT EXISTS idx_production_approval_dossier_created
ON production_promotion_approvals(dossier_id,created_ts_utc,approval_id);
"""


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _policy_material(policy: ProductionPromotionPolicy) -> dict[str, object]:
    return {
        "minimum_shadow_soak_seconds": policy.minimum_shadow_soak.total_seconds(),
        "maximum_training_age_seconds": policy.maximum_training_age.total_seconds(),
        "maximum_live_evidence_age_seconds": policy.maximum_live_evidence_age.total_seconds(),
        "minimum_canary_pairs": policy.minimum_canary_pairs,
        "minimum_capacity_clip_multiplier": policy.minimum_capacity_clip_multiplier,
        "dossier_ttl_seconds": policy.dossier_ttl.total_seconds(),
        "required_roles": [role.value for role in policy.required_roles],
        "require_distinct_operators": policy.require_distinct_operators,
    }


def production_policy_fingerprint(policy: ProductionPromotionPolicy) -> str:
    return _hash_payload({"version": "production-promotion-policy-v1", **_policy_material(policy)})


def production_evidence_fingerprint(evidence: ProductionGateEvidence) -> str:
    return _hash_payload(
        {
            "version": "production-promotion-evidence-v1",
            "component": evidence.component,
            "retraining_plan": asdict(evidence.retraining_plan),
            "training": asdict(evidence.training),
            "shadow": asdict(evidence.shadow),
            "promotion": asdict(evidence.promotion),
            "canary": asdict(evidence.canary),
            "capacity": asdict(evidence.capacity),
            "safety_state": asdict(evidence.safety_state),
            "runtime_certified": evidence.runtime_certified,
            "drift_active": evidence.drift_active,
            "policy_fingerprint": evidence.policy_fingerprint,
            "research_manifest_hash": evidence.research_manifest_hash,
            "canary_observed_ts_utc": evidence.canary_observed_ts_utc.astimezone(UTC).isoformat(),
            "capacity_observed_ts_utc": (
                evidence.capacity_observed_ts_utc.astimezone(UTC).isoformat()
            ),
            "runtime_certified_ts_utc": (
                evidence.runtime_certified_ts_utc.astimezone(UTC).isoformat()
            ),
            "observed_ts_utc": evidence.observed_ts_utc.astimezone(UTC).isoformat(),
        }
    )


def _check_freshness(
    *,
    name: str,
    evidence_ts: datetime,
    observed: datetime,
    maximum_age: timedelta,
    failures: list[str],
) -> None:
    normalized = evidence_ts.astimezone(UTC)
    if normalized > observed:
        failures.append(f"{name}_timestamp_in_future")
    elif observed - normalized > maximum_age:
        failures.append(f"{name}_evidence_stale")


def build_production_promotion_dossier(
    evidence: ProductionGateEvidence,
    *,
    policy: ProductionPromotionPolicy | None = None,
) -> ProductionPromotionDossier:
    policy = policy or ProductionPromotionPolicy()
    observed = evidence.observed_ts_utc.astimezone(UTC)
    failures: list[str] = []

    if not evidence.retraining_plan.ready:
        failures.append("retraining_plan_not_ready")
    if evidence.retraining_plan.dataset_fingerprint != evidence.training.dataset_fingerprint:
        failures.append("retraining_plan_dataset_mismatch")
    if not evidence.training.shadow_ready:
        failures.append("training_run_not_shadow_ready")
    if evidence.shadow.component != evidence.component:
        failures.append("shadow_component_mismatch")
    if evidence.shadow.training_run_id != evidence.training.run_id:
        failures.append("shadow_training_run_mismatch")
    if evidence.shadow.artifact_sha256 != evidence.training.artifact_sha256:
        failures.append("shadow_artifact_mismatch")
    if evidence.shadow.state is not ShadowReleaseState.ACTIVE_SHADOW:
        failures.append("shadow_release_not_active")
    if evidence.promotion.status is not PromotionStatus.READY_FOR_OPERATOR_REVIEW:
        failures.append("independent_promotion_evidence_not_ready")
    if evidence.canary.status is not CanaryStatus.READY_FOR_OPERATOR_REVIEW:
        failures.append("live_shadow_canary_not_ready")
    if evidence.canary.pairs < policy.minimum_canary_pairs:
        failures.append(
            f"canary_pairs:{evidence.canary.pairs}<{policy.minimum_canary_pairs}"
        )
    if not evidence.capacity.robust:
        failures.append("liquidity_capacity_not_robust")
    if evidence.capacity.max_robust_clip_multiplier < policy.minimum_capacity_clip_multiplier:
        failures.append(
            "capacity_clip_multiplier:"
            f"{evidence.capacity.max_robust_clip_multiplier:.6f}"
            f"<{policy.minimum_capacity_clip_multiplier:.6f}"
        )
    if evidence.safety_state.component != evidence.component:
        failures.append("safety_latch_component_mismatch")
    if evidence.safety_state.mode is not SafetyMode.NORMAL:
        failures.append("component_fail_closed_no_trade")
    if not evidence.runtime_certified:
        failures.append("runtime_not_certified")
    if evidence.drift_active:
        failures.append("drift_active_at_production_gate")

    training_ts = evidence.training.created_ts_utc.astimezone(UTC)
    if training_ts > observed:
        failures.append("training_timestamp_in_future")
    elif observed - training_ts > policy.maximum_training_age:
        failures.append("training_artifact_too_old")

    _check_freshness(
        name="canary",
        evidence_ts=evidence.canary_observed_ts_utc,
        observed=observed,
        maximum_age=policy.maximum_live_evidence_age,
        failures=failures,
    )
    _check_freshness(
        name="capacity",
        evidence_ts=evidence.capacity_observed_ts_utc,
        observed=observed,
        maximum_age=policy.maximum_live_evidence_age,
        failures=failures,
    )
    _check_freshness(
        name="runtime_certification",
        evidence_ts=evidence.runtime_certified_ts_utc,
        observed=observed,
        maximum_age=policy.maximum_live_evidence_age,
        failures=failures,
    )

    shadow_ts = evidence.shadow.activated_ts_utc
    if shadow_ts is None:
        failures.append("shadow_activation_timestamp_missing")
    else:
        shadow_ts = shadow_ts.astimezone(UTC)
        if shadow_ts > observed:
            failures.append("shadow_activation_timestamp_in_future")
        elif observed - shadow_ts < policy.minimum_shadow_soak:
            failures.append(
                "shadow_soak_seconds:"
                f"{(observed - shadow_ts).total_seconds():.3f}"
                f"<{policy.minimum_shadow_soak.total_seconds():.3f}"
            )
        if evidence.canary_observed_ts_utc.astimezone(UTC) < shadow_ts:
            failures.append("canary_evidence_predates_shadow_activation")

    evidence_hash = production_evidence_fingerprint(evidence)
    policy_hash = production_policy_fingerprint(policy)
    status = (
        ProductionGateStatus.READY_FOR_APPROVAL
        if not failures
        else ProductionGateStatus.BLOCKED
    )
    dossier_id = _hash_payload(
        {
            "version": "production-promotion-dossier-v1",
            "evidence_hash": evidence_hash,
            "policy_hash": policy_hash,
        }
    )
    return ProductionPromotionDossier(
        dossier_id=dossier_id,
        component=evidence.component,
        training_run_id=evidence.training.run_id,
        shadow_release_id=evidence.shadow.shadow_release_id,
        artifact_sha256=evidence.training.artifact_sha256,
        parent_release_id=evidence.shadow.parent_release_id,
        evidence_hash=evidence_hash,
        policy_hash=policy_hash,
        created_ts_utc=observed,
        expires_ts_utc=observed + policy.dossier_ttl,
        status=status,
        failures=tuple(failures),
    )


class ProductionGateRegistry:
    """Durable two-person production approval ledger bound to one evidence snapshot."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.executescript(_SCHEMA)

    def persist_dossier(self, dossier: ProductionPromotionDossier) -> bool:
        encoded = json.dumps(
            asdict(dossier), sort_keys=True, separators=(",", ":"), default=str
        )
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.path) as db:
            db.executescript(_SCHEMA)
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO production_promotion_dossiers
                (dossier_id,component,evidence_hash,policy_hash,status,expires_ts_utc,
                 dossier_json,dossier_sha256,created_ts_utc)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    dossier.dossier_id,
                    dossier.component,
                    dossier.evidence_hash,
                    dossier.policy_hash,
                    dossier.status.value,
                    dossier.expires_ts_utc.isoformat(),
                    encoded,
                    digest,
                    dossier.created_ts_utc.isoformat(),
                ),
            )
        self._verify_dossier_integrity(dossier.dossier_id)
        return cursor.rowcount == 1

    def _verify_dossier_integrity(self, dossier_id: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """
                SELECT dossier_json,dossier_sha256
                FROM production_promotion_dossiers WHERE dossier_id=?
                """,
                (dossier_id,),
            ).fetchone()
        if row is None:
            raise KeyError(dossier_id)
        encoded = str(row["dossier_json"])
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if digest != str(row["dossier_sha256"]):
            raise ValueError("production dossier ledger integrity mismatch")

    def approve(
        self,
        dossier_id: str,
        *,
        role: ProductionApprovalRole,
        operator: str,
        now: datetime | None = None,
    ) -> ProductionApproval:
        if not operator.strip():
            raise ValueError("operator identity is required")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        self._verify_dossier_integrity(dossier_id)
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            dossier = db.execute(
                """
                SELECT status,expires_ts_utc FROM production_promotion_dossiers
                WHERE dossier_id=?
                """,
                (dossier_id,),
            ).fetchone()
            if dossier is None:
                raise KeyError(dossier_id)
            dossier_status = ProductionGateStatus(str(dossier["status"]))
            if dossier_status is not ProductionGateStatus.READY_FOR_APPROVAL:
                raise ValueError("blocked production dossier cannot be approved")
            expires = datetime.fromisoformat(str(dossier["expires_ts_utc"])).astimezone(UTC)
            if timestamp > expires:
                raise ValueError("production dossier has expired")
            existing_role = db.execute(
                """
                SELECT operator FROM production_promotion_approvals
                WHERE dossier_id=? AND role=?
                """,
                (dossier_id, role.value),
            ).fetchone()
            if existing_role is not None:
                raise ValueError("approval role is already satisfied")
            existing_operator = db.execute(
                """
                SELECT role FROM production_promotion_approvals
                WHERE dossier_id=? AND operator=?
                """,
                (dossier_id, operator),
            ).fetchone()
            if existing_operator is not None:
                raise ValueError("one operator cannot satisfy multiple production approval roles")
            approval_id = _hash_payload(
                {
                    "version": "production-approval-v1",
                    "dossier_id": dossier_id,
                    "role": role.value,
                    "operator": operator,
                    "created_ts_utc": timestamp.isoformat(),
                }
            )
            db.execute(
                """
                INSERT INTO production_promotion_approvals
                (approval_id,dossier_id,role,operator,created_ts_utc)
                VALUES (?,?,?,?,?)
                """,
                (approval_id, dossier_id, role.value, operator, timestamp.isoformat()),
            )
        return ProductionApproval(approval_id, dossier_id, role, operator, timestamp)

    def approvals(self, dossier_id: str) -> tuple[ProductionApproval, ...]:
        self._verify_dossier_integrity(dossier_id)
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT * FROM production_promotion_approvals
                WHERE dossier_id=? ORDER BY created_ts_utc,approval_id
                """,
                (dossier_id,),
            ).fetchall()
        return tuple(
            ProductionApproval(
                approval_id=str(row["approval_id"]),
                dossier_id=str(row["dossier_id"]),
                role=ProductionApprovalRole(str(row["role"])),
                operator=str(row["operator"]),
                created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
            )
            for row in rows
        )

    def evaluate_authorization(
        self,
        dossier: ProductionPromotionDossier,
        *,
        current_evidence: ProductionGateEvidence,
        policy: ProductionPromotionPolicy | None = None,
        now: datetime | None = None,
    ) -> ProductionAuthorization:
        policy = policy or ProductionPromotionPolicy()
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        failures: list[str] = []
        self._verify_dossier_integrity(dossier.dossier_id)

        if timestamp > dossier.expires_ts_utc:
            return ProductionAuthorization(
                authorization_id="",
                dossier_id=dossier.dossier_id,
                status=ProductionAuthorizationStatus.EXPIRED,
                approvals=self.approvals(dossier.dossier_id),
                authorized_until_ts_utc=None,
                failures=("production_dossier_expired",),
            )
        if not dossier.ready_for_approval:
            failures.append("production_dossier_blocked")
        if production_policy_fingerprint(policy) != dossier.policy_hash:
            failures.append("production_policy_changed_since_dossier")
        if production_evidence_fingerprint(current_evidence) != dossier.evidence_hash:
            failures.append("production_evidence_changed_since_dossier")

        approvals = self.approvals(dossier.dossier_id)
        by_role = {item.role: item for item in approvals}
        missing = [role for role in policy.required_roles if role not in by_role]
        if missing:
            failures.append("missing_approval_roles:" + ",".join(role.value for role in missing))
        if policy.require_distinct_operators:
            operators = [by_role[role].operator for role in policy.required_roles if role in by_role]
            if len(operators) != len(set(operators)):
                failures.append("production_approvers_not_distinct")

        if failures:
            status = (
                ProductionAuthorizationStatus.PENDING_APPROVALS
                if all(item.startswith("missing_approval_roles:") for item in failures)
                else ProductionAuthorizationStatus.BLOCKED
            )
            return ProductionAuthorization(
                authorization_id="",
                dossier_id=dossier.dossier_id,
                status=status,
                approvals=approvals,
                authorized_until_ts_utc=None,
                failures=tuple(failures),
            )

        authorization_id = _hash_payload(
            {
                "version": "production-authorization-v1",
                "dossier_id": dossier.dossier_id,
                "approval_ids": [item.approval_id for item in approvals],
                "authorized_until_ts_utc": dossier.expires_ts_utc.isoformat(),
            }
        )
        return ProductionAuthorization(
            authorization_id=authorization_id,
            dossier_id=dossier.dossier_id,
            status=ProductionAuthorizationStatus.AUTHORIZED_FOR_OPERATOR_ACTIVATION,
            approvals=approvals,
            authorized_until_ts_utc=dossier.expires_ts_utc,
            failures=(),
        )

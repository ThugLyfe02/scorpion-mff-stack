from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .production_gate import (
    ProductionAuthorization,
    ProductionGateEvidence,
    ProductionGateRegistry,
    ProductionPromotionDossier,
    ProductionPromotionPolicy,
    production_evidence_fingerprint,
)


class PromotionEvidenceKind(StrEnum):
    RETRAINING_PLAN = "RETRAINING_PLAN"
    TRAINING_RUN = "TRAINING_RUN"
    SHADOW_RELEASE = "SHADOW_RELEASE"
    PROMOTION_DECISION = "PROMOTION_DECISION"
    LIVE_CANARY = "LIVE_CANARY"
    LIQUIDITY_CAPACITY = "LIQUIDITY_CAPACITY"
    SAFETY_LATCH = "SAFETY_LATCH"
    RUNTIME_CERTIFICATION = "RUNTIME_CERTIFICATION"
    DRIFT_STATE = "DRIFT_STATE"
    POLICY_IDENTITY = "POLICY_IDENTITY"
    RESEARCH_MANIFEST = "RESEARCH_MANIFEST"


@dataclass(frozen=True, slots=True)
class PromotionEvidenceEnvelope:
    evidence_id: str
    kind: PromotionEvidenceKind
    subject_id: str
    schema_version: str
    producer: str
    observed_ts_utc: datetime
    expires_ts_utc: datetime | None
    payload_sha256: str
    parent_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("evidence_id", "subject_id", "schema_version", "producer", "payload_sha256"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if self.observed_ts_utc.tzinfo is None or self.observed_ts_utc.utcoffset() is None:
            raise ValueError("observed_ts_utc must be timezone-aware")
        if self.expires_ts_utc is not None:
            if self.expires_ts_utc.tzinfo is None or self.expires_ts_utc.utcoffset() is None:
                raise ValueError("expires_ts_utc must be timezone-aware")
            if self.expires_ts_utc < self.observed_ts_utc:
                raise ValueError("expires_ts_utc cannot precede observed_ts_utc")
        if len(self.parent_evidence_ids) != len(set(self.parent_evidence_ids)):
            raise ValueError("parent evidence ids must be unique")


@dataclass(frozen=True, slots=True)
class PromotionEvidenceBundle:
    schema_version: str
    component: str
    source_evidence_hash: str
    records: tuple[PromotionEvidenceEnvelope, ...]
    created_ts_utc: datetime
    bundle_hash: str


@dataclass(frozen=True, slots=True)
class PromotionEvidenceSchemaPolicy:
    schema_version: str = "promotion-evidence-v1"
    maximum_record_age: timedelta = timedelta(days=14)
    require_exactly_one_per_kind: bool = True

    def __post_init__(self) -> None:
        if not self.schema_version.strip():
            raise ValueError("schema_version is required")
        if self.maximum_record_age <= timedelta(0):
            raise ValueError("maximum_record_age must be positive")


@dataclass(frozen=True, slots=True)
class PromotionEvidenceValidationReport:
    valid: bool
    records: int
    required_kinds: int
    failures: tuple[str, ...]


_REQUIRED_KINDS = tuple(PromotionEvidenceKind)


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _envelope(
    *,
    kind: PromotionEvidenceKind,
    subject_id: str,
    payload: object,
    producer: str,
    observed_ts_utc: datetime,
    expires_ts_utc: datetime | None = None,
    parents: tuple[str, ...] = (),
    schema_version: str,
) -> PromotionEvidenceEnvelope:
    payload_sha = _hash_payload(payload)
    material = {
        "kind": kind.value,
        "subject_id": subject_id,
        "schema_version": schema_version,
        "producer": producer,
        "observed_ts_utc": observed_ts_utc.astimezone(UTC).isoformat(),
        "expires_ts_utc": (
            expires_ts_utc.astimezone(UTC).isoformat() if expires_ts_utc is not None else None
        ),
        "payload_sha256": payload_sha,
        "parent_evidence_ids": list(parents),
    }
    return PromotionEvidenceEnvelope(
        evidence_id=_hash_payload({"version": "promotion-envelope-v1", **material}),
        kind=kind,
        subject_id=subject_id,
        schema_version=schema_version,
        producer=producer,
        observed_ts_utc=observed_ts_utc.astimezone(UTC),
        expires_ts_utc=(expires_ts_utc.astimezone(UTC) if expires_ts_utc is not None else None),
        payload_sha256=payload_sha,
        parent_evidence_ids=parents,
    )


def build_promotion_evidence_bundle(
    evidence: ProductionGateEvidence,
    *,
    production_policy: ProductionPromotionPolicy | None = None,
    schema_policy: PromotionEvidenceSchemaPolicy | None = None,
) -> PromotionEvidenceBundle:
    production_policy = production_policy or ProductionPromotionPolicy()
    schema_policy = schema_policy or PromotionEvidenceSchemaPolicy()
    observed = evidence.observed_ts_utc.astimezone(UTC)
    records: list[PromotionEvidenceEnvelope] = []

    retraining = _envelope(
        kind=PromotionEvidenceKind.RETRAINING_PLAN,
        subject_id=evidence.retraining_plan.plan_id,
        payload=asdict(evidence.retraining_plan),
        producer="drift-retraining",
        observed_ts_utc=evidence.retraining_plan.created_ts_utc,
        schema_version=schema_policy.schema_version,
    )
    records.append(retraining)
    training = _envelope(
        kind=PromotionEvidenceKind.TRAINING_RUN,
        subject_id=evidence.training.run_id,
        payload=asdict(evidence.training),
        producer="auto-trainer",
        observed_ts_utc=evidence.training.created_ts_utc,
        parents=(retraining.evidence_id,),
        schema_version=schema_policy.schema_version,
    )
    records.append(training)
    shadow_ts = evidence.shadow.activated_ts_utc or evidence.shadow.created_ts_utc
    shadow = _envelope(
        kind=PromotionEvidenceKind.SHADOW_RELEASE,
        subject_id=evidence.shadow.shadow_release_id,
        payload=asdict(evidence.shadow),
        producer="shadow-lifecycle",
        observed_ts_utc=shadow_ts,
        parents=(training.evidence_id,),
        schema_version=schema_policy.schema_version,
    )
    records.append(shadow)
    records.append(
        _envelope(
            kind=PromotionEvidenceKind.PROMOTION_DECISION,
            subject_id=evidence.training.run_id,
            payload=asdict(evidence.promotion),
            producer="governance",
            observed_ts_utc=observed,
            parents=(training.evidence_id,),
            schema_version=schema_policy.schema_version,
        )
    )
    records.append(
        _envelope(
            kind=PromotionEvidenceKind.LIVE_CANARY,
            subject_id=evidence.shadow.shadow_release_id,
            payload=asdict(evidence.canary),
            producer="live-shadow-canary",
            observed_ts_utc=evidence.canary_observed_ts_utc,
            expires_ts_utc=(
                evidence.canary_observed_ts_utc + production_policy.maximum_live_evidence_age
            ),
            parents=(shadow.evidence_id,),
            schema_version=schema_policy.schema_version,
        )
    )
    records.append(
        _envelope(
            kind=PromotionEvidenceKind.LIQUIDITY_CAPACITY,
            subject_id=evidence.training.run_id,
            payload=asdict(evidence.capacity),
            producer="liquidity-capacity",
            observed_ts_utc=evidence.capacity_observed_ts_utc,
            expires_ts_utc=(
                evidence.capacity_observed_ts_utc + production_policy.maximum_live_evidence_age
            ),
            parents=(training.evidence_id,),
            schema_version=schema_policy.schema_version,
        )
    )
    records.append(
        _envelope(
            kind=PromotionEvidenceKind.SAFETY_LATCH,
            subject_id=evidence.component,
            payload=asdict(evidence.safety_state),
            producer="fail-safe-control",
            observed_ts_utc=evidence.safety_state.updated_ts_utc,
            expires_ts_utc=(
                evidence.safety_state.updated_ts_utc + production_policy.maximum_live_evidence_age
            ),
            schema_version=schema_policy.schema_version,
        )
    )
    records.append(
        _envelope(
            kind=PromotionEvidenceKind.RUNTIME_CERTIFICATION,
            subject_id=evidence.component,
            payload={"runtime_certified": evidence.runtime_certified},
            producer="runtime-certification",
            observed_ts_utc=evidence.runtime_certified_ts_utc,
            expires_ts_utc=(
                evidence.runtime_certified_ts_utc + production_policy.maximum_live_evidence_age
            ),
            schema_version=schema_policy.schema_version,
        )
    )
    records.append(
        _envelope(
            kind=PromotionEvidenceKind.DRIFT_STATE,
            subject_id=evidence.component,
            payload={"drift_active": evidence.drift_active},
            producer="drift-control",
            observed_ts_utc=observed,
            expires_ts_utc=observed + production_policy.maximum_live_evidence_age,
            parents=(training.evidence_id,),
            schema_version=schema_policy.schema_version,
        )
    )
    records.append(
        _envelope(
            kind=PromotionEvidenceKind.POLICY_IDENTITY,
            subject_id=evidence.policy_fingerprint,
            payload={"policy_fingerprint": evidence.policy_fingerprint},
            producer="policy-bundle",
            observed_ts_utc=observed,
            schema_version=schema_policy.schema_version,
        )
    )
    records.append(
        _envelope(
            kind=PromotionEvidenceKind.RESEARCH_MANIFEST,
            subject_id=evidence.research_manifest_hash,
            payload={"research_manifest_hash": evidence.research_manifest_hash},
            producer="research-manifest",
            observed_ts_utc=observed,
            parents=(training.evidence_id,),
            schema_version=schema_policy.schema_version,
        )
    )

    source_hash = production_evidence_fingerprint(evidence)
    material = {
        "version": schema_policy.schema_version,
        "component": evidence.component,
        "source_evidence_hash": source_hash,
        "records": [asdict(item) for item in records],
        "created_ts_utc": observed.isoformat(),
    }
    return PromotionEvidenceBundle(
        schema_version=schema_policy.schema_version,
        component=evidence.component,
        source_evidence_hash=source_hash,
        records=tuple(records),
        created_ts_utc=observed,
        bundle_hash=_hash_payload(material),
    )


def validate_promotion_evidence_bundle(
    bundle: PromotionEvidenceBundle,
    *,
    now: datetime,
    policy: PromotionEvidenceSchemaPolicy | None = None,
) -> PromotionEvidenceValidationReport:
    policy = policy or PromotionEvidenceSchemaPolicy()
    now_utc = now.astimezone(UTC)
    failures: list[str] = []
    if bundle.schema_version != policy.schema_version:
        failures.append("promotion_evidence_schema_version_mismatch")
    if not bundle.component.strip() or not bundle.source_evidence_hash.strip():
        failures.append("promotion_evidence_bundle_identity_missing")

    by_id = {item.evidence_id: item for item in bundle.records}
    if len(by_id) != len(bundle.records):
        failures.append("duplicate_promotion_evidence_id")
    counts = {kind: 0 for kind in _REQUIRED_KINDS}
    for item in bundle.records:
        counts[item.kind] += 1
        if item.schema_version != bundle.schema_version:
            failures.append(f"record_schema_version_mismatch:{item.kind.value}")
        if item.observed_ts_utc > now_utc:
            failures.append(f"future_evidence:{item.kind.value}")
        elif now_utc - item.observed_ts_utc > policy.maximum_record_age:
            failures.append(f"stale_evidence:{item.kind.value}")
        if item.expires_ts_utc is not None and now_utc > item.expires_ts_utc:
            failures.append(f"expired_evidence:{item.kind.value}")
        for parent in item.parent_evidence_ids:
            if parent not in by_id:
                failures.append(f"missing_parent_evidence:{item.kind.value}:{parent}")

    for kind, count in counts.items():
        if count == 0:
            failures.append(f"missing_evidence_kind:{kind.value}")
        elif policy.require_exactly_one_per_kind and count != 1:
            failures.append(f"evidence_kind_count:{kind.value}:{count}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visited:
            return
        if identifier in visiting:
            failures.append(f"promotion_evidence_cycle:{identifier}")
            return
        visiting.add(identifier)
        row = by_id.get(identifier)
        if row is not None:
            for parent in row.parent_evidence_ids:
                visit(parent)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in by_id:
        visit(identifier)

    material = {
        "version": bundle.schema_version,
        "component": bundle.component,
        "source_evidence_hash": bundle.source_evidence_hash,
        "records": [asdict(item) for item in bundle.records],
        "created_ts_utc": bundle.created_ts_utc.astimezone(UTC).isoformat(),
    }
    if _hash_payload(material) != bundle.bundle_hash:
        failures.append("promotion_evidence_bundle_hash_mismatch")
    return PromotionEvidenceValidationReport(
        valid=not failures,
        records=len(bundle.records),
        required_kinds=len(_REQUIRED_KINDS),
        failures=tuple(failures),
    )


def evaluate_formalized_production_authorization(
    registry: ProductionGateRegistry,
    dossier: ProductionPromotionDossier,
    *,
    current_evidence: ProductionGateEvidence,
    bundle: PromotionEvidenceBundle,
    production_policy: ProductionPromotionPolicy | None = None,
    schema_policy: PromotionEvidenceSchemaPolicy | None = None,
    now: datetime | None = None,
) -> ProductionAuthorization:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    report = validate_promotion_evidence_bundle(bundle, now=timestamp, policy=schema_policy)
    if not report.valid:
        raise ValueError("formal promotion evidence invalid: " + ";".join(report.failures))
    expected = production_evidence_fingerprint(current_evidence)
    if bundle.source_evidence_hash != expected:
        raise ValueError("formal promotion evidence is not bound to current evidence")
    return registry.evaluate_authorization(
        dossier,
        current_evidence=current_evidence,
        policy=production_policy,
        now=timestamp,
    )

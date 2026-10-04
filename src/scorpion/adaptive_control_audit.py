from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .live_sizing_safety import LiveRiskState, LiveSizingDecision, LiveSizingRequest
from .production_gate import (
    ProductionAuthorization,
    ProductionAuthorizationStatus,
    ProductionGateEvidence,
    ProductionPromotionDossier,
    production_evidence_fingerprint,
)
from .promotion_evidence_schema import (
    PromotionEvidenceBundle,
    PromotionEvidenceSchemaPolicy,
    validate_promotion_evidence_bundle,
)


@dataclass(frozen=True, slots=True)
class AdaptiveControlAuditReport:
    passed: bool
    identity_failures: tuple[str, ...]
    temporal_failures: tuple[str, ...]
    authority_failures: tuple[str, ...]
    failures: tuple[str, ...]


def audit_adaptive_control_path(
    *,
    evidence: ProductionGateEvidence,
    bundle: PromotionEvidenceBundle,
    dossier: ProductionPromotionDossier,
    authorization: ProductionAuthorization,
    sizing_request: LiveSizingRequest,
    risk_state: LiveRiskState,
    sizing_decision: LiveSizingDecision,
    now: datetime,
    schema_policy: PromotionEvidenceSchemaPolicy | None = None,
) -> AdaptiveControlAuditReport:
    """Check identity, temporal, and authority continuity across the adaptive control path."""
    now_utc = now.astimezone(UTC)
    identity: list[str] = []
    temporal: list[str] = []
    authority: list[str] = []

    schema = validate_promotion_evidence_bundle(bundle, now=now_utc, policy=schema_policy)
    if not schema.valid:
        identity.extend(f"promotion_schema:{item}" for item in schema.failures)
    current_hash = production_evidence_fingerprint(evidence)
    if bundle.source_evidence_hash != current_hash:
        identity.append("formal_bundle_not_bound_to_current_production_evidence")
    if dossier.evidence_hash != current_hash:
        identity.append("dossier_not_bound_to_current_production_evidence")
    if dossier.training_run_id != evidence.training.run_id:
        identity.append("dossier_training_run_mismatch")
    if dossier.shadow_release_id != evidence.shadow.shadow_release_id:
        identity.append("dossier_shadow_release_mismatch")
    if dossier.artifact_sha256 != evidence.training.artifact_sha256:
        identity.append("dossier_artifact_mismatch")
    if risk_state.active_release_id and sizing_request.release_id != risk_state.active_release_id:
        identity.append("sizing_request_not_bound_to_active_release")
    if sizing_decision.request_id != sizing_request.request_id:
        identity.append("sizing_decision_request_mismatch")

    training_ts = evidence.training.created_ts_utc.astimezone(UTC)
    shadow_ts = evidence.shadow.activated_ts_utc
    if shadow_ts is not None:
        shadow_ts = shadow_ts.astimezone(UTC)
        if training_ts > shadow_ts:
            temporal.append("shadow_activation_predates_training")
        if evidence.canary_observed_ts_utc.astimezone(UTC) < shadow_ts:
            temporal.append("canary_predates_shadow_activation")
    if evidence.canary_observed_ts_utc.astimezone(UTC) > dossier.created_ts_utc.astimezone(UTC):
        temporal.append("dossier_predates_canary_evidence")
    if dossier.created_ts_utc.astimezone(UTC) > sizing_request.created_ts_utc.astimezone(UTC):
        temporal.append("sizing_request_predates_production_dossier")
    if sizing_request.created_ts_utc.astimezone(UTC) > risk_state.observed_ts_utc.astimezone(UTC):
        temporal.append("risk_snapshot_predates_sizing_request")
    if risk_state.observed_ts_utc.astimezone(UTC) > now_utc:
        temporal.append("risk_snapshot_timestamp_in_future")
    decision_expiry = sizing_decision.valid_until_ts_utc.astimezone(UTC)
    risk_observed = risk_state.observed_ts_utc.astimezone(UTC)
    if decision_expiry < risk_observed:
        temporal.append("sizing_decision_expired_before_risk_snapshot")

    if authorization.status is not ProductionAuthorizationStatus.AUTHORIZED_FOR_OPERATOR_ACTIVATION:
        authority.append("production_not_authorized_for_operator_activation")
    if authorization.dossier_id != dossier.dossier_id:
        authority.append("production_authorization_dossier_mismatch")
    if authorization.authorized_until_ts_utc is None:
        authority.append("production_authorization_has_no_expiry")
    elif sizing_request.created_ts_utc.astimezone(UTC) > authorization.authorized_until_ts_utc:
        authority.append("sizing_request_created_after_production_authorization_expiry")
    if not sizing_decision.permitted:
        authority.append("live_sizing_request_not_permitted")
    if evidence.safety_state.execution_allowed is False:
        authority.append("production_evidence_safety_latch_blocks_execution")
    if risk_state.safety_state.execution_allowed is False:
        authority.append("live_risk_state_safety_latch_blocks_execution")

    failures = tuple((*identity, *temporal, *authority))
    return AdaptiveControlAuditReport(
        passed=not failures,
        identity_failures=tuple(identity),
        temporal_failures=tuple(temporal),
        authority_failures=tuple(authority),
        failures=failures,
    )

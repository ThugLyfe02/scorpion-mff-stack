from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .selective import SelectivePolicy
from .tournament import CandidateScore
from .walk_forward import WalkForwardReport


class PromotionStatus(StrEnum):
    READY_FOR_OPERATOR_REVIEW = "READY_FOR_OPERATOR_REVIEW"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    candidate: CandidateScore
    selective_policy: SelectivePolicy
    walk_forward: WalkForwardReport
    research_manifest_hash: str
    online_drifted: bool = False
    dataset_complete: bool = False
    quote_coverage_ok: bool = False
    depth_coverage_ok: bool = False


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    status: PromotionStatus
    failures: tuple[str, ...]
    reason: str


def evaluate_promotion(evidence: PromotionEvidence) -> PromotionDecision:
    """Decide whether a candidate has enough evidence for *human* promotion review.

    This function never deploys or activates a parser/model/strategy. It exists to keep
    research, validation, and deployment authority separated even as the system becomes more
    automated.
    """
    failures: list[str] = []
    if not evidence.candidate.qualified:
        failures.append("candidate_tournament_not_qualified")
        failures.extend(f"candidate:{item}" for item in evidence.candidate.failures)
    if not evidence.selective_policy.enabled:
        failures.append("selective_risk_policy_not_empirically_enabled")
    if not evidence.walk_forward.passed:
        failures.append("walk_forward_out_of_sample_failed")
        failures.extend(f"walk_forward:{item}" for item in evidence.walk_forward.failures)
    if not evidence.research_manifest_hash.strip():
        failures.append("research_manifest_missing")
    if evidence.online_drifted:
        failures.append("online_drift_active")
    if not evidence.dataset_complete:
        failures.append("dataset_not_complete")
    if not evidence.quote_coverage_ok:
        failures.append("quote_coverage_insufficient")
    if not evidence.depth_coverage_ok:
        failures.append("depth_coverage_insufficient")

    if failures:
        return PromotionDecision(
            PromotionStatus.BLOCKED,
            tuple(failures),
            "candidate remains research-only until every independent evidence gate passes",
        )
    return PromotionDecision(
        PromotionStatus.READY_FOR_OPERATOR_REVIEW,
        (),
        (
            "all automated evidence gates passed; explicit operator review is still required "
            "before any production policy/model change"
        ),
    )

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from .uncertainty_decomposition import UncertaintyDecomposition, UncertaintyStatus


class LearningAction(StrEnum):
    DEFER = "DEFER"
    LIGHT_SHADOW = "LIGHT_SHADOW"
    DEEP_SHADOW = "DEEP_SHADOW"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class YieldCalibration(Protocol):
    @property
    def calibration_hash(self) -> str: ...

    def multiplier_for(self, action: str, segment: str = "") -> float: ...


@dataclass(frozen=True, slots=True)
class InformationValuePolicy:
    epistemic_weight: float = 0.30
    variation_weight: float = 0.10
    residual_hotspot_weight: float = 0.20
    label_scarcity_weight: float = 0.12
    coverage_deficit_weight: float = 0.10
    novelty_weight: float = 0.08
    actionable_disagreement_weight: float = 0.10
    aleatoric_discount: float = 0.75
    inherently_ambiguous_threshold: float = 0.65
    low_epistemic_threshold: float = 0.15
    minimum_light_utility: float = 0.12
    minimum_deep_utility: float = 0.25
    minimum_review_utility: float = 0.30
    light_cost_units: int = 1
    deep_cost_units: int = 5
    review_cost_units: int = 8
    maximum_budget_units: int = 10_000

    def __post_init__(self) -> None:
        weights = (
            self.epistemic_weight,
            self.variation_weight,
            self.residual_hotspot_weight,
            self.label_scarcity_weight,
            self.coverage_deficit_weight,
            self.novelty_weight,
            self.actionable_disagreement_weight,
        )
        if any(value < 0 for value in weights):
            raise ValueError("information-value weights cannot be negative")
        if sum(weights) <= 0:
            raise ValueError("at least one information-value weight must be positive")
        for name in (
            "aleatoric_discount",
            "inherently_ambiguous_threshold",
            "low_epistemic_threshold",
            "minimum_light_utility",
            "minimum_deep_utility",
            "minimum_review_utility",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        if min(self.light_cost_units, self.deep_cost_units, self.review_cost_units) <= 0:
            raise ValueError("learning action costs must be positive")
        if self.maximum_budget_units <= 0:
            raise ValueError("maximum_budget_units must be positive")


@dataclass(frozen=True, slots=True)
class LearningSignal:
    event_id: str
    uncertainty: UncertaintyDecomposition
    residual_hotspot_priority: float = 0.0
    label_scarcity: float = 0.0
    coverage_deficit: float = 0.0
    novelty: float = 0.0
    actionable_disagreement: bool = False
    yield_segment: str = ""

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id is required")
        if not math.isfinite(self.residual_hotspot_priority) or self.residual_hotspot_priority < 0:
            raise ValueError("residual_hotspot_priority must be finite and non-negative")
        for name in ("label_scarcity", "coverage_deficit", "novelty"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be finite and in [0,1]")
        if self.yield_segment and not self.yield_segment.strip():
            raise ValueError("yield_segment cannot be whitespace")


@dataclass(frozen=True, slots=True)
class LearningValueAssessment:
    event_id: str
    expected_information_value: float
    learnability: float
    ambiguity_discount: float
    light_utility: float
    deep_utility: float
    review_utility: float
    intrinsically_ambiguous: bool
    reasons: tuple[str, ...]
    yield_segment: str = ""


@dataclass(frozen=True, slots=True)
class LearningAllocationDecision:
    event_id: str
    action: LearningAction
    cost_units: int
    utility: float
    yield_multiplier: float = 1.0
    base_utility: float = 0.0


@dataclass(frozen=True, slots=True)
class LearningBudgetAllocation:
    decisions: tuple[LearningAllocationDecision, ...]
    budget_units: int
    used_units: int
    total_utility: float
    allocation_hash: str
    yield_calibration_hash: str = ""


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _hotspot_strength(priority: float) -> float:
    return 1.0 - math.exp(-priority)


def assess_information_value(
    signal: LearningSignal,
    *,
    policy: InformationValuePolicy | None = None,
) -> LearningValueAssessment:
    """Estimate research value without granting runtime or brokerage authority."""
    policy = policy or InformationValuePolicy()
    uncertainty = signal.uncertainty
    epistemic = _clamp(uncertainty.normalized_epistemic)
    aleatoric = _clamp(uncertainty.normalized_aleatoric)
    learnability = epistemic * (1.0 - aleatoric)
    hotspot = _hotspot_strength(signal.residual_hotspot_priority)
    disagreement = 1.0 if signal.actionable_disagreement else 0.0
    weight_total = (
        policy.epistemic_weight
        + policy.variation_weight
        + policy.residual_hotspot_weight
        + policy.label_scarcity_weight
        + policy.coverage_deficit_weight
        + policy.novelty_weight
        + policy.actionable_disagreement_weight
    )
    raw = (
        policy.epistemic_weight * learnability
        + policy.variation_weight * _clamp(uncertainty.variation_ratio)
        + policy.residual_hotspot_weight * hotspot
        + policy.label_scarcity_weight * signal.label_scarcity
        + policy.coverage_deficit_weight * signal.coverage_deficit
        + policy.novelty_weight * signal.novelty
        + policy.actionable_disagreement_weight * disagreement
    ) / weight_total
    ambiguity_discount = _clamp(1.0 - policy.aleatoric_discount * aleatoric)
    value = _clamp(raw * ambiguity_discount)
    intrinsically_ambiguous = (
        aleatoric >= policy.inherently_ambiguous_threshold
        and epistemic < policy.low_epistemic_threshold
    )
    if intrinsically_ambiguous:
        value *= 0.25

    light = _clamp(value * (0.80 + 0.20 * (1.0 - epistemic)))
    deep = _clamp(value * (1.0 + 0.35 * epistemic + 0.15 * signal.novelty))
    review = _clamp(
        value
        * (1.0 + 0.65 * disagreement + 0.30 * hotspot + 0.20 * signal.coverage_deficit)
    )
    reasons: list[str] = []
    if uncertainty.status in {UncertaintyStatus.CONFLICTED, UncertaintyStatus.NEEDS_MORE_DATA}:
        reasons.append("epistemic_uncertainty")
    if hotspot >= 0.25:
        reasons.append("robust_residual_hotspot")
    if signal.label_scarcity >= 0.50:
        reasons.append("label_scarcity")
    if signal.coverage_deficit >= 0.50:
        reasons.append("coverage_deficit")
    if signal.actionable_disagreement:
        reasons.append("actionable_disagreement")
    if intrinsically_ambiguous:
        reasons.append("intrinsic_ambiguity_discount")
    if not reasons:
        reasons.append("low_marginal_information")
    return LearningValueAssessment(
        event_id=signal.event_id,
        expected_information_value=value,
        learnability=learnability,
        ambiguity_discount=ambiguity_discount,
        light_utility=light,
        deep_utility=deep,
        review_utility=review,
        intrinsically_ambiguous=intrinsically_ambiguous,
        reasons=tuple(reasons),
        yield_segment=signal.yield_segment,
    )


def _scaled_decision(
    assessment: LearningValueAssessment,
    action: LearningAction,
    *,
    cost_units: int,
    base_utility: float,
    calibration: YieldCalibration | None,
) -> LearningAllocationDecision:
    multiplier = (
        calibration.multiplier_for(action.value, assessment.yield_segment)
        if calibration is not None
        else 1.0
    )
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError("yield calibration multiplier must be finite and positive")
    return LearningAllocationDecision(
        event_id=assessment.event_id,
        action=action,
        cost_units=cost_units,
        utility=base_utility * multiplier,
        yield_multiplier=multiplier,
        base_utility=base_utility,
    )


def _options(
    assessment: LearningValueAssessment,
    policy: InformationValuePolicy,
    calibration: YieldCalibration | None,
) -> tuple[LearningAllocationDecision, ...]:
    options = [
        LearningAllocationDecision(
            assessment.event_id,
            LearningAction.DEFER,
            0,
            0.0,
            1.0,
            0.0,
        )
    ]
    if assessment.intrinsically_ambiguous:
        return tuple(options)
    if assessment.light_utility >= policy.minimum_light_utility:
        options.append(
            _scaled_decision(
                assessment,
                LearningAction.LIGHT_SHADOW,
                cost_units=policy.light_cost_units,
                base_utility=assessment.light_utility,
                calibration=calibration,
            )
        )
    if assessment.deep_utility >= policy.minimum_deep_utility:
        options.append(
            _scaled_decision(
                assessment,
                LearningAction.DEEP_SHADOW,
                cost_units=policy.deep_cost_units,
                base_utility=assessment.deep_utility,
                calibration=calibration,
            )
        )
    if assessment.review_utility >= policy.minimum_review_utility:
        options.append(
            _scaled_decision(
                assessment,
                LearningAction.HUMAN_REVIEW,
                cost_units=policy.review_cost_units,
                base_utility=assessment.review_utility,
                calibration=calibration,
            )
        )
    return tuple(options)


def allocate_learning_budget(
    assessments: tuple[LearningValueAssessment, ...],
    *,
    budget_units: int,
    policy: InformationValuePolicy | None = None,
    yield_calibration: YieldCalibration | None = None,
) -> LearningBudgetAllocation:
    """Solve an exact research knapsack, optionally calibrated by realized learning yield."""
    policy = policy or InformationValuePolicy()
    if budget_units < 0 or budget_units > policy.maximum_budget_units:
        raise ValueError("budget_units is outside the configured research envelope")
    event_ids = [item.event_id for item in assessments]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("assessments must contain unique event ids")

    states: dict[int, tuple[float, tuple[LearningAllocationDecision, ...]]] = {0: (0.0, ())}
    for assessment in sorted(assessments, key=lambda item: item.event_id):
        next_states: dict[int, tuple[float, tuple[LearningAllocationDecision, ...]]] = {}
        for used, (utility, decisions) in states.items():
            for option in _options(assessment, policy, yield_calibration):
                next_cost = used + option.cost_units
                if next_cost > budget_units:
                    continue
                candidate = (utility + option.utility, decisions + (option,))
                current = next_states.get(next_cost)
                if current is None or candidate[0] > current[0] + 1e-12:
                    next_states[next_cost] = candidate
                elif current is not None and abs(candidate[0] - current[0]) <= 1e-12:
                    candidate_key = tuple((item.event_id, item.action.value) for item in candidate[1])
                    current_key = tuple((item.event_id, item.action.value) for item in current[1])
                    if candidate_key < current_key:
                        next_states[next_cost] = candidate
        states = next_states
    best_cost, best = min(
        states.items(),
        key=lambda item: (-item[1][0], item[0], tuple(d.action.value for d in item[1][1])),
    )
    selected = tuple(item for item in best[1] if item.action is not LearningAction.DEFER)
    calibration_hash = yield_calibration.calibration_hash if yield_calibration is not None else ""
    material = {
        "version": "learning-budget-allocation-v2",
        "budget_units": budget_units,
        "used_units": best_cost,
        "policy": asdict(policy),
        "yield_calibration_hash": calibration_hash,
        "decisions": [
            {
                "event_id": item.event_id,
                "action": item.action.value,
                "cost_units": item.cost_units,
                "base_utility": round(item.base_utility, 12),
                "yield_multiplier": round(item.yield_multiplier, 12),
                "utility": round(item.utility, 12),
            }
            for item in selected
        ],
    }
    allocation_hash = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return LearningBudgetAllocation(
        decisions=selected,
        budget_units=budget_units,
        used_units=best_cost,
        total_utility=best[0],
        allocation_hash=allocation_hash,
        yield_calibration_hash=calibration_hash,
    )

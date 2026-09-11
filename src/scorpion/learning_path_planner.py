from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

from .counterfactual_learning_efficiency import (
    CounterfactualLearningReport,
    CounterfactualLearningStatus,
    CounterfactualSequenceEstimate,
)


@dataclass(frozen=True, slots=True)
class LearningPathPolicy:
    maximum_steps: int = 3
    maximum_budget_units: int = 20
    beam_width: int = 64
    maximum_alternatives: int = 5
    uncertainty_bonus_weight: float = 0.15
    maximum_uncertainty_bonus: float = 0.10
    unknown_transition_penalty: float = 0.05
    allow_repeated_treatments: bool = False

    def __post_init__(self) -> None:
        if min(
            self.maximum_steps,
            self.maximum_budget_units,
            self.beam_width,
            self.maximum_alternatives,
        ) <= 0:
            raise ValueError("learning path integer limits must be positive")
        for name in (
            "uncertainty_bonus_weight",
            "maximum_uncertainty_bonus",
            "unknown_transition_penalty",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class LearningPathStep:
    treatment_key: str
    cost_units: int
    conservative_base_value: float
    transition_value: float
    uncertainty_bonus: float
    step_value: float


@dataclass(frozen=True, slots=True)
class LearningPathCandidate:
    steps: tuple[LearningPathStep, ...]
    total_cost_units: int
    total_conservative_value: float
    path_hash: str


@dataclass(frozen=True, slots=True)
class LearningPathPlan:
    counterfactual_report_hash: str
    reward_contract_hash: str
    current_regime: str
    best: LearningPathCandidate | None
    alternatives: tuple[LearningPathCandidate, ...]
    plan_hash: str
    failures: tuple[str, ...]


def _hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _transition_map(
    rows: tuple[CounterfactualSequenceEstimate, ...],
) -> dict[tuple[str, str], CounterfactualSequenceEstimate]:
    return {
        (row.previous_treatment_key, row.current_treatment_key): row
        for row in rows
    }


def _path_candidate(steps: tuple[LearningPathStep, ...]) -> LearningPathCandidate:
    material = {
        "version": "learning-path-candidate-v1",
        "steps": [asdict(item) for item in steps],
    }
    return LearningPathCandidate(
        steps=steps,
        total_cost_units=sum(item.cost_units for item in steps),
        total_conservative_value=sum(item.step_value for item in steps),
        path_hash=_hash(material),
    )


def plan_learning_path(
    report: CounterfactualLearningReport,
    *,
    policy: LearningPathPolicy | None = None,
) -> LearningPathPlan:
    """Plan a research-only sequence from conservative causal learning-value evidence."""
    policy = policy or LearningPathPolicy()
    failures: list[str] = []
    if report.status is not CounterfactualLearningStatus.QUALIFIED:
        failures.append("counterfactual_learning_report_not_qualified")
    estimates = {
        item.treatment_key: item
        for item in report.treatment_estimates
        if not item.treatment_key.startswith("CONTROL@") and item.cost_units > 0
    }
    if not estimates:
        failures.append("no_noncontrol_learning_treatment_available")
    if failures:
        return _plan_result(report, None, (), policy, tuple(failures))

    transitions = _transition_map(report.sequence_estimates)
    beam: list[tuple[tuple[LearningPathStep, ...], int, float]] = [((), 0, 0.0)]
    completed: list[LearningPathCandidate] = []
    for _ in range(policy.maximum_steps):
        expanded: list[tuple[tuple[LearningPathStep, ...], int, float]] = []
        for steps, used, value in beam:
            used_keys = {item.treatment_key for item in steps}
            for key in sorted(estimates):
                if not policy.allow_repeated_treatments and key in used_keys:
                    continue
                estimate = estimates[key]
                next_cost = used + estimate.cost_units
                if next_cost > policy.maximum_budget_units:
                    continue
                bonus = min(
                    policy.maximum_uncertainty_bonus,
                    policy.uncertainty_bonus_weight * estimate.posterior_std_error,
                )
                transition_value = 0.0
                if steps:
                    previous = steps[-1].treatment_key
                    transition = transitions.get((previous, key))
                    transition_value = (
                        transition.simultaneous_synergy_lower_bound
                        if transition is not None
                        else -policy.unknown_transition_penalty
                    )
                step_value = estimate.simultaneous_lower_bound + transition_value + bonus
                step = LearningPathStep(
                    treatment_key=key,
                    cost_units=estimate.cost_units,
                    conservative_base_value=estimate.simultaneous_lower_bound,
                    transition_value=transition_value,
                    uncertainty_bonus=bonus,
                    step_value=step_value,
                )
                candidate_steps = steps + (step,)
                candidate_value = value + step_value
                expanded.append((candidate_steps, next_cost, candidate_value))
                completed.append(_path_candidate(candidate_steps))
        if not expanded:
            break
        expanded.sort(
            key=lambda item: (
                -item[2],
                item[1],
                tuple(step.treatment_key for step in item[0]),
            )
        )
        beam = expanded[: policy.beam_width]

    unique: dict[str, LearningPathCandidate] = {}
    for candidate in completed:
        unique[candidate.path_hash] = candidate
    ranked = sorted(
        unique.values(),
        key=lambda item: (
            -item.total_conservative_value,
            item.total_cost_units,
            tuple(step.treatment_key for step in item.steps),
        ),
    )
    best = ranked[0] if ranked else None
    alternatives = tuple(ranked[1 : policy.maximum_alternatives + 1])
    return _plan_result(report, best, alternatives, policy, ())


def _plan_result(
    report: CounterfactualLearningReport,
    best: LearningPathCandidate | None,
    alternatives: tuple[LearningPathCandidate, ...],
    policy: LearningPathPolicy,
    failures: tuple[str, ...],
) -> LearningPathPlan:
    material = {
        "version": "learning-path-plan-v1",
        "counterfactual_report_hash": report.report_hash,
        "reward_contract_hash": report.reward_contract_hash,
        "current_regime": report.current_regime,
        "policy": asdict(policy),
        "best": asdict(best) if best is not None else None,
        "alternatives": [asdict(item) for item in alternatives],
        "failures": failures,
    }
    return LearningPathPlan(
        counterfactual_report_hash=report.report_hash,
        reward_contract_hash=report.reward_contract_hash,
        current_regime=report.current_regime,
        best=best,
        alternatives=alternatives,
        plan_hash=_hash(material),
        failures=failures,
    )

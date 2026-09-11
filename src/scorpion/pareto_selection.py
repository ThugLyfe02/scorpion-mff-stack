from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class ParetoStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class MultiObjectiveCandidate:
    candidate_id: str
    accuracy: float
    calibration_error: float
    latency_ms: float
    execution_robustness: float
    complexity: float
    false_action_rate: float = 0.0
    wrong_action_rate: float = 0.0

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id is required")
        for name in (
            "accuracy",
            "calibration_error",
            "execution_robustness",
            "false_action_rate",
            "wrong_action_rate",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        if self.latency_ms < 0 or self.complexity < 0:
            raise ValueError("latency_ms and complexity cannot be negative")
        if not all(
            math.isfinite(value)
            for value in (
                self.accuracy,
                self.calibration_error,
                self.latency_ms,
                self.execution_robustness,
                self.complexity,
                self.false_action_rate,
                self.wrong_action_rate,
            )
        ):
            raise ValueError("candidate objectives must be finite")


@dataclass(frozen=True, slots=True)
class ParetoSelectionPolicy:
    minimum_candidates: int = 2
    minimum_accuracy: float = 0.95
    maximum_calibration_error: float = 0.10
    maximum_latency_ms: float = 20.0
    minimum_execution_robustness: float = 0.80
    maximum_false_action_rate: float = 0.005
    maximum_wrong_action_rate: float = 0.01
    epsilon_accuracy: float = 1e-4
    epsilon_calibration: float = 1e-4
    epsilon_latency_ms: float = 0.01
    epsilon_execution_robustness: float = 1e-4
    epsilon_complexity: float = 1e-4

    def __post_init__(self) -> None:
        if self.minimum_candidates < 2:
            raise ValueError("minimum_candidates must be >=2")
        for name in (
            "minimum_accuracy",
            "maximum_calibration_error",
            "minimum_execution_robustness",
            "maximum_false_action_rate",
            "maximum_wrong_action_rate",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        if self.maximum_latency_ms <= 0:
            raise ValueError("maximum_latency_ms must be positive")
        for name in (
            "epsilon_accuracy",
            "epsilon_calibration",
            "epsilon_latency_ms",
            "epsilon_execution_robustness",
            "epsilon_complexity",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class CandidateParetoEvidence:
    candidate_id: str
    safety_eligible: bool
    safety_failures: tuple[str, ...]
    pareto_optimal: bool
    dominated_by: tuple[str, ...]
    worst_normalized_regret: float | None


@dataclass(frozen=True, slots=True)
class ParetoSelectionReport:
    candidates: int
    safety_eligible: int
    frontier: tuple[str, ...]
    balanced_champion: str | None
    evidence: tuple[CandidateParetoEvidence, ...]
    status: ParetoStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is ParetoStatus.QUALIFIED


def _safety_failures(
    candidate: MultiObjectiveCandidate,
    policy: ParetoSelectionPolicy,
) -> tuple[str, ...]:
    failures: list[str] = []
    if candidate.accuracy < policy.minimum_accuracy:
        failures.append("accuracy_below_floor")
    if candidate.calibration_error > policy.maximum_calibration_error:
        failures.append("calibration_error_above_ceiling")
    if candidate.latency_ms > policy.maximum_latency_ms:
        failures.append("latency_above_ceiling")
    if candidate.execution_robustness < policy.minimum_execution_robustness:
        failures.append("execution_robustness_below_floor")
    if candidate.false_action_rate > policy.maximum_false_action_rate:
        failures.append("false_action_rate_above_ceiling")
    if candidate.wrong_action_rate > policy.maximum_wrong_action_rate:
        failures.append("wrong_action_rate_above_ceiling")
    return tuple(failures)


def _dominates(
    left: MultiObjectiveCandidate,
    right: MultiObjectiveCandidate,
    policy: ParetoSelectionPolicy,
) -> bool:
    no_worse = (
        left.accuracy + policy.epsilon_accuracy >= right.accuracy
        and left.calibration_error <= right.calibration_error + policy.epsilon_calibration
        and left.latency_ms <= right.latency_ms + policy.epsilon_latency_ms
        and left.execution_robustness + policy.epsilon_execution_robustness
        >= right.execution_robustness
        and left.complexity <= right.complexity + policy.epsilon_complexity
    )
    materially_better = (
        left.accuracy > right.accuracy + policy.epsilon_accuracy
        or left.calibration_error + policy.epsilon_calibration < right.calibration_error
        or left.latency_ms + policy.epsilon_latency_ms < right.latency_ms
        or left.execution_robustness
        > right.execution_robustness + policy.epsilon_execution_robustness
        or left.complexity + policy.epsilon_complexity < right.complexity
    )
    return no_worse and materially_better


def _normalized_regret(
    candidate: MultiObjectiveCandidate,
    eligible: tuple[MultiObjectiveCandidate, ...],
) -> float:
    def regret(value: float, ideal: float, worst: float, maximize: bool) -> float:
        span = abs(ideal - worst)
        if span <= 1e-12:
            return 0.0
        raw = (ideal - value) / span if maximize else (value - ideal) / span
        return max(0.0, min(1.0, raw))

    accuracies = [item.accuracy for item in eligible]
    calibrations = [item.calibration_error for item in eligible]
    latencies = [item.latency_ms for item in eligible]
    robustness = [item.execution_robustness for item in eligible]
    complexities = [item.complexity for item in eligible]
    regrets = (
        regret(candidate.accuracy, max(accuracies), min(accuracies), True),
        regret(candidate.calibration_error, min(calibrations), max(calibrations), False),
        regret(candidate.latency_ms, min(latencies), max(latencies), False),
        regret(candidate.execution_robustness, max(robustness), min(robustness), True),
        regret(candidate.complexity, min(complexities), max(complexities), False),
    )
    return max(regrets)


def evaluate_pareto_selection(
    candidates: tuple[MultiObjectiveCandidate, ...],
    *,
    policy: ParetoSelectionPolicy | None = None,
) -> ParetoSelectionReport:
    """Build a safety-constrained Pareto frontier without arbitrary weighted scalarization.

    Hard safety/quality floors are applied before dominance. The optional balanced champion is a
    research convenience chosen by minimax normalized regret to the observed ideal point; the
    complete Pareto frontier remains visible and no candidate is deployed by this component.
    """
    policy = policy or ParetoSelectionPolicy()
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise ValueError("candidate ids must be unique")
    safety = {item.candidate_id: _safety_failures(item, policy) for item in candidates}
    eligible = tuple(item for item in candidates if not safety[item.candidate_id])
    failures: list[str] = []
    if len(candidates) < policy.minimum_candidates:
        failures.append(
            f"insufficient_candidates:{len(candidates)}<{policy.minimum_candidates}"
        )
    if not eligible:
        failures.append("no_safety_eligible_candidate")
    if failures:
        return ParetoSelectionReport(
            candidates=len(candidates),
            safety_eligible=len(eligible),
            frontier=(),
            balanced_champion=None,
            evidence=tuple(
                CandidateParetoEvidence(
                    candidate_id=item.candidate_id,
                    safety_eligible=not safety[item.candidate_id],
                    safety_failures=safety[item.candidate_id],
                    pareto_optimal=False,
                    dominated_by=(),
                    worst_normalized_regret=None,
                )
                for item in sorted(candidates, key=lambda row: row.candidate_id)
            ),
            status=ParetoStatus.INSUFFICIENT,
            failures=tuple(failures),
        )

    dominated_by: dict[str, tuple[str, ...]] = {}
    for candidate in eligible:
        dominators = tuple(
            sorted(
                other.candidate_id
                for other in eligible
                if other.candidate_id != candidate.candidate_id
                and _dominates(other, candidate, policy)
            )
        )
        dominated_by[candidate.candidate_id] = dominators
    frontier_candidates = tuple(
        item for item in eligible if not dominated_by[item.candidate_id]
    )
    regrets = {
        item.candidate_id: _normalized_regret(item, eligible)
        for item in frontier_candidates
    }
    champion = (
        min(
            frontier_candidates,
            key=lambda item: (regrets[item.candidate_id], item.complexity, item.candidate_id),
        ).candidate_id
        if frontier_candidates
        else None
    )
    evidence = tuple(
        CandidateParetoEvidence(
            candidate_id=item.candidate_id,
            safety_eligible=not safety[item.candidate_id],
            safety_failures=safety[item.candidate_id],
            pareto_optimal=(
                not safety[item.candidate_id]
                and not dominated_by.get(item.candidate_id, ())
            ),
            dominated_by=dominated_by.get(item.candidate_id, ()),
            worst_normalized_regret=regrets.get(item.candidate_id),
        )
        for item in sorted(candidates, key=lambda row: row.candidate_id)
    )
    return ParetoSelectionReport(
        candidates=len(candidates),
        safety_eligible=len(eligible),
        frontier=tuple(sorted(item.candidate_id for item in frontier_candidates)),
        balanced_champion=champion,
        evidence=evidence,
        status=ParetoStatus.QUALIFIED,
        failures=(),
    )

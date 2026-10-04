from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class CanaryStatus(StrEnum):
    OBSERVING = "OBSERVING"
    READY_FOR_OPERATOR_REVIEW = "READY_FOR_OPERATOR_REVIEW"
    QUARANTINE = "QUARANTINE"


@dataclass(frozen=True, slots=True)
class PairedCanaryObservation:
    champion_correct: bool
    challenger_correct: bool
    champion_actionable: bool
    challenger_actionable: bool
    contract_diverged: bool
    challenger_latency_us: int
    champion_latency_us: int

    def __post_init__(self) -> None:
        if self.challenger_latency_us < 0 or self.champion_latency_us < 0:
            raise ValueError("latencies cannot be negative")


@dataclass(frozen=True, slots=True)
class CanaryPolicy:
    minimum_pairs: int = 100
    maximum_action_escalations: int = 0
    maximum_contract_divergences: int = 0
    minimum_accuracy_delta: float = -0.005
    maximum_latency_ratio: float = 1.50
    confidence_z: float = 1.959963984540054


@dataclass(frozen=True, slots=True)
class CanaryReport:
    status: CanaryStatus
    pairs: int
    champion_accuracy: float
    challenger_accuracy: float
    paired_accuracy_delta: float
    delta_lower_95: float
    action_escalations: int
    contract_divergences: int
    latency_ratio: float
    failures: tuple[str, ...]


def _paired_delta_lower_bound(
    observations: tuple[PairedCanaryObservation, ...],
    z: float,
) -> float:
    if not observations:
        return -1.0
    deltas = [
        float(item.challenger_correct) - float(item.champion_correct)
        for item in observations
    ]
    mean = sum(deltas) / len(deltas)
    if len(deltas) == 1:
        return mean - 1.0
    variance = sum((value - mean) ** 2 for value in deltas) / (len(deltas) - 1)
    standard_error = math.sqrt(variance / len(deltas))
    return mean - z * standard_error


def evaluate_canary(
    observations: tuple[PairedCanaryObservation, ...],
    *,
    policy: CanaryPolicy | None = None,
) -> CanaryReport:
    policy = policy or CanaryPolicy()
    pairs = len(observations)
    champion_accuracy = (
        sum(item.champion_correct for item in observations) / pairs if pairs else 0.0
    )
    challenger_accuracy = (
        sum(item.challenger_correct for item in observations) / pairs if pairs else 0.0
    )
    delta = challenger_accuracy - champion_accuracy
    lower = _paired_delta_lower_bound(observations, policy.confidence_z)
    action_escalations = sum(
        not item.champion_actionable and item.challenger_actionable for item in observations
    )
    contract_divergences = sum(item.contract_diverged for item in observations)
    champion_latency = sum(item.champion_latency_us for item in observations)
    challenger_latency = sum(item.challenger_latency_us for item in observations)
    latency_ratio = (
        challenger_latency / champion_latency
        if champion_latency > 0
        else (1.0 if challenger_latency == 0 else math.inf)
    )

    failures: list[str] = []
    if action_escalations > policy.maximum_action_escalations:
        failures.append(
            f"action_escalations:{action_escalations}>{policy.maximum_action_escalations}"
        )
    if contract_divergences > policy.maximum_contract_divergences:
        failures.append(
            f"contract_divergences:{contract_divergences}>{policy.maximum_contract_divergences}"
        )
    if latency_ratio > policy.maximum_latency_ratio:
        failures.append(f"latency_ratio:{latency_ratio:.3f}>{policy.maximum_latency_ratio:.3f}")

    dangerous = bool(failures)
    if dangerous:
        status = CanaryStatus.QUARANTINE
    elif pairs < policy.minimum_pairs:
        status = CanaryStatus.OBSERVING
        failures.append(f"pairs:{pairs}<{policy.minimum_pairs}")
    elif lower < policy.minimum_accuracy_delta:
        status = CanaryStatus.OBSERVING
        failures.append(
            f"accuracy_delta_lower_95:{lower:.6f}<{policy.minimum_accuracy_delta:.6f}"
        )
    else:
        status = CanaryStatus.READY_FOR_OPERATOR_REVIEW

    return CanaryReport(
        status=status,
        pairs=pairs,
        champion_accuracy=champion_accuracy,
        challenger_accuracy=challenger_accuracy,
        paired_accuracy_delta=delta,
        delta_lower_95=lower,
        action_escalations=action_escalations,
        contract_divergences=contract_divergences,
        latency_ratio=latency_ratio,
        failures=tuple(failures),
    )

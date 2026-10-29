from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .accuracy import DecisionEvidence


class CalibrationStatus(StrEnum):
    UNCALIBRATED = "UNCALIBRATED"
    PROVISIONAL = "PROVISIONAL"
    TRUSTED = "TRUSTED"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    rule_id: str
    stated_confidence: float
    correct: bool
    actionable: bool = False


@dataclass(frozen=True, slots=True)
class RuleCalibration:
    rule_id: str
    samples: int
    correct: int
    empirical_accuracy: float
    wilson_lower_95: float
    mean_stated_confidence: float
    brier_score: float
    calibration_gap: float


@dataclass(frozen=True, slots=True)
class CalibratedDecision:
    rule_id: str
    stated_confidence: float
    effective_confidence: float
    lower_bound: float | None
    samples: int
    status: CalibrationStatus


def wilson_lower_bound(successes: int, total: int, *, z: float = 1.959963984540054) -> float:
    if total < 0 or successes < 0 or successes > total:
        raise ValueError("invalid successes/total")
    if total == 0:
        return 0.0
    p = successes / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    radius = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return max(0.0, (centre - radius) / denominator)


def calibrate_rules(observations: Sequence[CalibrationObservation]) -> dict[str, RuleCalibration]:
    grouped: dict[str, list[CalibrationObservation]] = defaultdict(list)
    for observation in observations:
        if not 0.0 <= observation.stated_confidence <= 1.0:
            raise ValueError("stated_confidence must be between 0 and 1")
        grouped[observation.rule_id].append(observation)

    result: dict[str, RuleCalibration] = {}
    for rule_id, rows in grouped.items():
        samples = len(rows)
        correct = sum(row.correct for row in rows)
        empirical = correct / samples
        mean_confidence = sum(row.stated_confidence for row in rows) / samples
        brier = sum(
            (row.stated_confidence - (1.0 if row.correct else 0.0)) ** 2 for row in rows
        ) / samples
        result[rule_id] = RuleCalibration(
            rule_id=rule_id,
            samples=samples,
            correct=correct,
            empirical_accuracy=empirical,
            wilson_lower_95=wilson_lower_bound(correct, samples),
            mean_stated_confidence=mean_confidence,
            brier_score=brier,
            calibration_gap=mean_confidence - empirical,
        )
    return result


def assess_rule_confidence(
    evidence: DecisionEvidence,
    calibration: RuleCalibration | None,
    *,
    min_samples: int = 25,
    required_lower_bound: float = 0.95,
) -> CalibratedDecision:
    if min_samples <= 0:
        raise ValueError("min_samples must be positive")
    if not 0.0 <= required_lower_bound <= 1.0:
        raise ValueError("required_lower_bound must be between 0 and 1")
    if calibration is None:
        return CalibratedDecision(
            rule_id=evidence.rule_id,
            stated_confidence=evidence.confidence,
            effective_confidence=evidence.confidence,
            lower_bound=None,
            samples=0,
            status=CalibrationStatus.UNCALIBRATED,
        )

    if calibration.samples < min_samples:
        effective = min(evidence.confidence, calibration.empirical_accuracy)
        status = CalibrationStatus.PROVISIONAL
    else:
        effective = min(evidence.confidence, calibration.wilson_lower_95)
        status = (
            CalibrationStatus.TRUSTED
            if calibration.wilson_lower_95 >= required_lower_bound
            else CalibrationStatus.DEGRADED
        )
    return CalibratedDecision(
        rule_id=evidence.rule_id,
        stated_confidence=evidence.confidence,
        effective_confidence=effective,
        lower_bound=calibration.wilson_lower_95,
        samples=calibration.samples,
        status=status,
    )

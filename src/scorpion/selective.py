from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SelectiveObservation:
    confidence: float
    correct: bool
    actionable: bool = False


@dataclass(frozen=True, slots=True)
class RiskCoveragePoint:
    threshold: float
    accepted: int
    total: int
    coverage: float
    errors: int
    empirical_error: float
    error_upper_95: float


@dataclass(frozen=True, slots=True)
class SelectivePolicy:
    enabled: bool
    threshold: float
    accepted: int
    coverage: float
    error_upper_95: float
    reason: str


def wilson_upper_bound(errors: int, total: int, *, z: float = 1.959963984540054) -> float:
    if total < 0 or errors < 0 or errors > total:
        raise ValueError("invalid errors/total")
    if total == 0:
        return 1.0
    p = errors / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    radius = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return min(1.0, (centre + radius) / denominator)


def risk_coverage_curve(
    observations: Sequence[SelectiveObservation],
    *,
    actionable_only: bool = False,
) -> tuple[RiskCoveragePoint, ...]:
    rows = [row for row in observations if not actionable_only or row.actionable]
    for row in rows:
        if not 0.0 <= row.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
    ordered = sorted(rows, key=lambda row: row.confidence, reverse=True)
    total = len(ordered)
    if total == 0:
        return ()

    errors = 0
    points: list[RiskCoveragePoint] = []
    for index, row in enumerate(ordered, start=1):
        errors += int(not row.correct)
        next_confidence = ordered[index].confidence if index < total else None
        if next_confidence is not None and next_confidence == row.confidence:
            continue
        points.append(
            RiskCoveragePoint(
                threshold=row.confidence,
                accepted=index,
                total=total,
                coverage=index / total,
                errors=errors,
                empirical_error=errors / index,
                error_upper_95=wilson_upper_bound(errors, index),
            )
        )
    return tuple(points)


def choose_selective_policy(
    observations: Sequence[SelectiveObservation],
    *,
    max_error_upper_95: float = 0.05,
    min_coverage: float = 0.20,
    min_samples: int = 50,
    actionable_only: bool = True,
) -> SelectivePolicy:
    if not 0.0 <= max_error_upper_95 <= 1.0:
        raise ValueError("max_error_upper_95 must be between 0 and 1")
    if not 0.0 <= min_coverage <= 1.0:
        raise ValueError("min_coverage must be between 0 and 1")
    if min_samples <= 0:
        raise ValueError("min_samples must be positive")

    candidates = [
        point
        for point in risk_coverage_curve(observations, actionable_only=actionable_only)
        if point.accepted >= min_samples
        and point.coverage >= min_coverage
        and point.error_upper_95 <= max_error_upper_95
    ]
    if not candidates:
        return SelectivePolicy(
            enabled=False,
            threshold=1.0,
            accepted=0,
            coverage=0.0,
            error_upper_95=1.0,
            reason="insufficient_evidence_for_target_risk",
        )
    best = max(candidates, key=lambda point: (point.coverage, -point.threshold))
    return SelectivePolicy(
        enabled=True,
        threshold=best.threshold,
        accepted=best.accepted,
        coverage=best.coverage,
        error_upper_95=best.error_upper_95,
        reason="empirical_risk_bound_satisfied",
    )

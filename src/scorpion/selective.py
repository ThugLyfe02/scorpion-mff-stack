from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from statistics import NormalDist


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
    simultaneous_error_upper_95: float = 1.0


@dataclass(frozen=True, slots=True)
class SelectivePolicy:
    enabled: bool
    threshold: float
    accepted: int
    coverage: float
    error_upper_95: float
    reason: str
    thresholds_tested: int = 0
    familywise_alpha: float = 0.05
    validation_samples: int = 0


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


def _selection_adjusted_z(thresholds: int, familywise_alpha: float) -> float:
    if thresholds <= 0:
        return 1.959963984540054
    # Preserve the prior two-sided-normal-equivalent 95% conservatism for one threshold,
    # then Bonferroni-adjust across every confidence threshold inspected on the same sample.
    tail_alpha = familywise_alpha / (2.0 * thresholds)
    return NormalDist().inv_cdf(1.0 - tail_alpha)


def risk_coverage_curve(
    observations: Sequence[SelectiveObservation],
    *,
    actionable_only: bool = False,
    familywise_alpha: float = 0.05,
) -> tuple[RiskCoveragePoint, ...]:
    if not 0.0 < familywise_alpha < 1.0:
        raise ValueError("familywise_alpha must be in (0,1)")
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

    adjusted_z = _selection_adjusted_z(len(points), familywise_alpha)
    return tuple(
        replace(
            point,
            simultaneous_error_upper_95=wilson_upper_bound(
                point.errors,
                point.accepted,
                z=adjusted_z,
            ),
        )
        for point in points
    )


def _fixed_threshold_point(
    observations: Sequence[SelectiveObservation],
    *,
    threshold: float,
    actionable_only: bool,
) -> RiskCoveragePoint:
    rows = [row for row in observations if not actionable_only or row.actionable]
    for row in rows:
        if not 0.0 <= row.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
    accepted_rows = [row for row in rows if row.confidence >= threshold]
    accepted = len(accepted_rows)
    total = len(rows)
    errors = sum(not row.correct for row in accepted_rows)
    pointwise = wilson_upper_bound(errors, accepted)
    return RiskCoveragePoint(
        threshold=threshold,
        accepted=accepted,
        total=total,
        coverage=accepted / total if total else 0.0,
        errors=errors,
        empirical_error=errors / accepted if accepted else 1.0,
        error_upper_95=pointwise,
        simultaneous_error_upper_95=pointwise,
    )


def choose_selective_policy(
    observations: Sequence[SelectiveObservation],
    *,
    max_error_upper_95: float = 0.05,
    min_coverage: float = 0.20,
    min_samples: int = 50,
    actionable_only: bool = True,
    familywise_alpha: float = 0.05,
    validation_observations: Sequence[SelectiveObservation] | None = None,
) -> SelectivePolicy:
    """Choose a confidence threshold without understating post-selection risk.

    With one dataset, every distinct threshold inspected is treated as a simultaneous family
    and the Wilson upper bound is Bonferroni-adjusted. When an independent validation sample is
    supplied, the threshold may be selected on the first sample and is then certified exactly
    once on the untouched validation sample with the ordinary fixed-threshold Wilson bound.
    """
    if not 0.0 <= max_error_upper_95 <= 1.0:
        raise ValueError("max_error_upper_95 must be between 0 and 1")
    if not 0.0 <= min_coverage <= 1.0:
        raise ValueError("min_coverage must be between 0 and 1")
    if min_samples <= 0:
        raise ValueError("min_samples must be positive")
    if not 0.0 < familywise_alpha < 1.0:
        raise ValueError("familywise_alpha must be in (0,1)")

    curve = risk_coverage_curve(
        observations,
        actionable_only=actionable_only,
        familywise_alpha=familywise_alpha,
    )
    if validation_observations is None:
        candidates = [
            point
            for point in curve
            if point.accepted >= min_samples
            and point.coverage >= min_coverage
            and point.simultaneous_error_upper_95 <= max_error_upper_95
        ]
        if not candidates:
            return SelectivePolicy(
                enabled=False,
                threshold=1.0,
                accepted=0,
                coverage=0.0,
                error_upper_95=1.0,
                reason="insufficient_selection_adjusted_evidence_for_target_risk",
                thresholds_tested=len(curve),
                familywise_alpha=familywise_alpha,
            )
        best = max(candidates, key=lambda point: (point.coverage, -point.threshold))
        return SelectivePolicy(
            enabled=True,
            threshold=best.threshold,
            accepted=best.accepted,
            coverage=best.coverage,
            error_upper_95=best.simultaneous_error_upper_95,
            reason="selection_adjusted_simultaneous_risk_bound_satisfied",
            thresholds_tested=len(curve),
            familywise_alpha=familywise_alpha,
        )

    # Threshold selection may use the discovery sample because the final risk claim is made only
    # on the independent validation sample after the threshold is fixed.
    discovery_candidates = [
        point
        for point in curve
        if point.accepted >= min_samples
        and point.coverage >= min_coverage
        and point.error_upper_95 <= max_error_upper_95
    ]
    if not discovery_candidates:
        return SelectivePolicy(
            enabled=False,
            threshold=1.0,
            accepted=0,
            coverage=0.0,
            error_upper_95=1.0,
            reason="discovery_sample_has_no_candidate_threshold",
            thresholds_tested=len(curve),
            familywise_alpha=familywise_alpha,
            validation_samples=len(validation_observations),
        )
    selected = max(
        discovery_candidates,
        key=lambda point: (point.coverage, -point.threshold),
    )
    validation = _fixed_threshold_point(
        validation_observations,
        threshold=selected.threshold,
        actionable_only=actionable_only,
    )
    validation_ok = (
        validation.accepted >= min_samples
        and validation.coverage >= min_coverage
        and validation.error_upper_95 <= max_error_upper_95
    )
    if not validation_ok:
        return SelectivePolicy(
            enabled=False,
            threshold=selected.threshold,
            accepted=validation.accepted,
            coverage=validation.coverage,
            error_upper_95=validation.error_upper_95,
            reason="independent_validation_did_not_support_target_risk",
            thresholds_tested=len(curve),
            familywise_alpha=familywise_alpha,
            validation_samples=validation.total,
        )
    return SelectivePolicy(
        enabled=True,
        threshold=selected.threshold,
        accepted=validation.accepted,
        coverage=validation.coverage,
        error_upper_95=validation.error_upper_95,
        reason="independent_holdout_risk_bound_satisfied",
        thresholds_tested=len(curve),
        familywise_alpha=familywise_alpha,
        validation_samples=validation.total,
    )

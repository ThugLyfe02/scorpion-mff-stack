from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .fill_calibration import FillCalibrationReport


class FillModelTrust(StrEnum):
    UNCALIBRATED = "UNCALIBRATED"
    TRUSTED = "TRUSTED"
    DEGRADED = "DEGRADED"
    UNTRUSTED = "UNTRUSTED"


@dataclass(frozen=True, slots=True)
class FillTrustPolicy:
    minimum_samples: int = 50
    minimum_interval_coverage_lower_bound: float = 0.98
    minimum_aggressive_samples: int = 25
    minimum_aggressive_exact_rate: float = 0.90
    maximum_total_violation_rate: float = 0.02
    confidence_z: float = 1.96

    def __post_init__(self) -> None:
        if self.minimum_samples <= 0 or self.minimum_aggressive_samples < 0:
            raise ValueError("sample thresholds must be non-negative and minimum_samples positive")
        for value in (
            self.minimum_interval_coverage_lower_bound,
            self.minimum_aggressive_exact_rate,
            self.maximum_total_violation_rate,
        ):
            if not 0 <= value <= 1:
                raise ValueError("probability thresholds must be in [0,1]")
        if self.confidence_z <= 0:
            raise ValueError("confidence_z must be positive")


@dataclass(frozen=True, slots=True)
class FillTrustAssessment:
    status: FillModelTrust
    samples: int
    interval_coverage_lower_bound: float
    observed_violation_rate: float
    aggressive_exact_rate: float
    reasons: tuple[str, ...]

    @property
    def allows_authoritative_research(self) -> bool:
        return self.status is FillModelTrust.TRUSTED


def _wilson_lower(successes: int, samples: int, z: float) -> float:
    if samples <= 0:
        return 0.0
    p = successes / samples
    z2 = z * z
    denominator = 1 + z2 / samples
    center = p + z2 / (2 * samples)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * samples)) / samples)
    return max(0.0, (center - margin) / denominator)


def assess_fill_model_trust(
    report: FillCalibrationReport,
    *,
    policy: FillTrustPolicy | None = None,
) -> FillTrustAssessment:
    policy = policy or FillTrustPolicy()
    if report.samples < policy.minimum_samples:
        return FillTrustAssessment(
            FillModelTrust.UNCALIBRATED,
            report.samples,
            0.0,
            0.0,
            report.aggressive_certified_exact_rate,
            (f"insufficient_calibration_samples:{report.samples}<{policy.minimum_samples}",),
        )

    violations = report.lower_bound_violations + report.upper_bound_violations
    successes = max(0, report.samples - violations)
    coverage_lower = _wilson_lower(successes, report.samples, policy.confidence_z)
    violation_rate = violations / report.samples if report.samples else 1.0
    reasons: list[str] = []

    if coverage_lower < policy.minimum_interval_coverage_lower_bound:
        reasons.append(
            "interval_coverage_lower_bound_below_threshold:"
            f"{coverage_lower:.6f}<{policy.minimum_interval_coverage_lower_bound:.6f}"
        )
    if violation_rate > policy.maximum_total_violation_rate:
        reasons.append(
            "fill_interval_violation_rate_above_threshold:"
            f"{violation_rate:.6f}>{policy.maximum_total_violation_rate:.6f}"
        )
    if report.aggressive_certified_samples < policy.minimum_aggressive_samples:
        reasons.append(
            "insufficient_aggressive_fill_samples:"
            f"{report.aggressive_certified_samples}<{policy.minimum_aggressive_samples}"
        )
    elif report.aggressive_certified_exact_rate < policy.minimum_aggressive_exact_rate:
        reasons.append(
            "aggressive_fill_exact_rate_below_threshold:"
            f"{report.aggressive_certified_exact_rate:.6f}<"
            f"{policy.minimum_aggressive_exact_rate:.6f}"
        )

    if not reasons:
        status = FillModelTrust.TRUSTED
    elif violation_rate > max(policy.maximum_total_violation_rate * 2, 0.05):
        status = FillModelTrust.UNTRUSTED
    else:
        status = FillModelTrust.DEGRADED

    return FillTrustAssessment(
        status=status,
        samples=report.samples,
        interval_coverage_lower_bound=coverage_lower,
        observed_violation_rate=violation_rate,
        aggressive_exact_rate=report.aggressive_certified_exact_rate,
        reasons=tuple(reasons),
    )

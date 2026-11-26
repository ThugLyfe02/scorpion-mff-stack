from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConfidenceSequence:
    samples: int
    successes: int
    empirical_rate: float
    lower_bound: float
    upper_bound: float
    alpha: float

    @property
    def width(self) -> float:
        return self.upper_bound - self.lower_bound


def anytime_bernoulli_confidence_sequence(
    successes: int,
    samples: int,
    *,
    alpha: float = 0.05,
) -> ConfidenceSequence:
    """Simple anytime-valid Bernoulli confidence sequence via a summable union bound.

    Fixed-sample intervals become optimistic when a deployment gate is checked after every new
    label and stopped as soon as it passes. This sequence allocates alpha over all sample sizes,
    making repeated monitoring materially safer. It is conservative by design.
    """
    if samples < 0 or successes < 0 or successes > samples:
        raise ValueError("invalid successes/samples")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0,1)")
    if samples == 0:
        return ConfidenceSequence(0, 0, 0.0, 0.0, 1.0, alpha)

    empirical = successes / samples
    # Sum_{n>=1} 1/[n(n+1)] = 1, so alpha_n below controls the full monitoring horizon.
    alpha_n = alpha / (samples * (samples + 1))
    radius = math.sqrt(math.log(2.0 / alpha_n) / (2.0 * samples))
    return ConfidenceSequence(
        samples=samples,
        successes=successes,
        empirical_rate=empirical,
        lower_bound=max(0.0, empirical - radius),
        upper_bound=min(1.0, empirical + radius),
        alpha=alpha,
    )


@dataclass(slots=True)
class OnlineBernoulliMonitor:
    alpha: float = 0.05
    samples: int = 0
    successes: int = 0

    def update(self, success: bool) -> ConfidenceSequence:
        self.samples += 1
        self.successes += int(success)
        return self.current()

    def current(self) -> ConfidenceSequence:
        return anytime_bernoulli_confidence_sequence(
            self.successes,
            self.samples,
            alpha=self.alpha,
        )

    def clears(self, required_lower_bound: float, *, min_samples: int = 1) -> bool:
        if min_samples <= 0:
            raise ValueError("min_samples must be positive")
        if not 0.0 <= required_lower_bound <= 1.0:
            raise ValueError("required_lower_bound must be between 0 and 1")
        current = self.current()
        return current.samples >= min_samples and current.lower_bound >= required_lower_bound


@dataclass(frozen=True, slots=True)
class BoundedMeanConfidenceSequence:
    """Bounds for the average predictable conditional mean of bounded session scores."""

    samples: int
    interval_mean_lower: float | None
    interval_mean_upper: float | None
    lower_bound: float
    upper_bound: float
    alpha: float
    radius: float | None
    fully_observed: int


def anytime_bounded_mean_confidence_sequence(
    observations: Sequence[tuple[float, float]],
    *, lower: float, upper: float, alpha: float = 0.05,
) -> BoundedMeanConfidenceSequence:
    """Hoeffding union-bound sequence for interval-observed bounded scores.

    If X_t lies in [lower,upper] and the supplied interval contains X_t, the
    confidence sequence covers the average of E[X_t | F_(t-1)] simultaneously
    over t, assuming the specified bounds and filtration. No within-session IID
    assumption is needed. It is not an interval for a stationary future mean.
    Missing observations may be represented by the full support, never zero.
    """
    if not all(math.isfinite(v) for v in (lower, upper, alpha)):
        raise ValueError("nonfinite confidence sequence parameters")
    if not lower < upper or not 0 < alpha < 1:
        raise ValueError("invalid confidence sequence support or alpha")
    if not math.isfinite(upper - lower):
        raise ValueError("confidence support width must be finite")
    n = len(observations)
    for lo, hi in observations:
        if not math.isfinite(lo) or not math.isfinite(hi) or not lower <= lo <= hi <= upper:
            raise ValueError("observation interval outside declared support")
    if not n:
        return BoundedMeanConfidenceSequence(0, None, None, lower, upper, alpha, None, 0)
    # Allocate alpha/(n(n+1)) to each two-sided look. Use logs to avoid underflow.
    radius = (upper - lower) * math.sqrt(
        ((math.log(2) - math.log(alpha)) + math.log(n) + math.log(n + 1)) / (2 * n)
    )
    lo_mean = math.fsum(lo / n for lo, _ in observations)
    hi_mean = math.fsum(hi / n for _, hi in observations)
    return BoundedMeanConfidenceSequence(
        n, lo_mean, hi_mean, max(lower, lo_mean - radius), min(upper, hi_mean + radius),
        alpha, radius, sum(lo == hi for lo, hi in observations),
    )

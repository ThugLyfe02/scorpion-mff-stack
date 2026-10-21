"""Drift detection: is the recent evidence still drawn from the corpus's world?

`experts.py` allocates risk by recent evidence. Nothing in it asks whether that evidence comes
from the same process that was calibrated. A weight that moves because the evidence changed is
adaptation; a weight that moves because the world changed is decay, and the weight path cannot
tell them apart.

The separation used here is the Wasserstein-1 distance between the current window of trade
outcomes and the reference distribution. Unlike a KS test it respects the MAGNITUDE of a shift,
not merely whether the distributions differ, which is what a sizing decision needs. In one
dimension the distance has a closed form - no transport solver - so it is exact:

    W1(P, Q) = integral over x of |F_P(x) - F_Q(x)| dx

evaluated exactly on the pooled sorted support.

The threshold is calibrated by resampling the reference itself, so the false-positive rate is
measured on in-distribution data rather than asserted. That matters: a guard that fires on
ordinary noise is ignored within a week, and an ignored guard is worse than none.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any


class DriftError(ValueError):
    """A drift question that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _sorted(values: Iterable[float]) -> list[float]:
    out = sorted(float(v) for v in values)
    if not out:
        raise DriftError("empty_sample", "no observations")
    return out


def wasserstein_1d(reference: Sequence[float], window: Sequence[float]) -> float:
    """Exact W1 between two 1-D empirical distributions, on the pooled support."""
    a = _sorted(reference)
    b = _sorted(window)
    support = sorted(set(a) | set(b))
    if len(support) == 1:
        return 0.0

    # Midpoint, not the right edge: sampling at `right` reads a step function AFTER its jump, which
    # attributes zero mass to the interval where the two distributions differ most.
    total = 0.0
    for left, right in zip(support, support[1:], strict=False):
        middle = 0.5 * (left + right)
        total += abs(_cdf_at(a, middle) - _cdf_at(b, middle)) * (right - left)
    return total


def _cdf_at(sorted_values: Sequence[float], point: float) -> float:
    """Right-continuous CDF at `point`, by binary search."""
    lo, hi = 0, len(sorted_values)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_values[mid] <= point + 1e-15:
            lo = mid + 1
        else:
            hi = mid
    return lo / len(sorted_values)


def windows(series: Sequence[float], size: int, step: int | None = None) -> list[list[float]]:
    """Overlapping windows of fixed size - the unit a drift test is run on."""
    if size < 2:
        raise DriftError("bad_size", str(size))
    stride = step or size
    if stride < 1:
        raise DriftError("bad_step", str(stride))
    return [
        list(series[i : i + size]) for i in range(0, len(series) - size + 1, stride)
    ]


@dataclass(frozen=True, slots=True)
class Detection:
    distance: float
    threshold: float
    size: int
    reference_size: int

    @property
    def drifted(self) -> bool:
        return self.distance > self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "distance": round(self.distance, 6),
            "threshold": round(self.threshold, 6),
            "window": self.size,
            "reference_size": self.reference_size,
            "drifted": self.drifted,
        }


def calibrate(
    reference: Sequence[float], *, size: int, quantile: float = 0.99,
    draws: int = 500, seed: int = 0,
) -> float:
    """Threshold from resampling the reference, so the false-positive rate is nominal."""
    base = _sorted(reference)
    if len(base) < size * 2:
        raise DriftError(
            "thin_reference", f"{len(base)} observations for windows of {size}"
        )
    if not (0.5 <= quantile < 1.0):
        raise DriftError("bad_quantile", str(quantile))
    rng = random.Random(seed)
    distances: list[float] = []
    for _ in range(draws):
        sample = [base[rng.randrange(len(base))] for _ in range(size)]
        distances.append(wasserstein_1d(base, sample))
    distances.sort()
    index = min(len(distances) - 1, int(quantile * len(distances)))
    return distances[index]


def detect(
    reference: Sequence[float], window: Sequence[float], *, threshold: float,
) -> Detection:
    if threshold <= 0:
        raise DriftError("bad_threshold", str(threshold))
    if len(window) < 2:
        raise DriftError("thin_window", str(len(window)))
    return Detection(
        distance=wasserstein_1d(reference, window),
        threshold=threshold,
        size=len(window),
        reference_size=len(reference),
    )


def latest(series: Sequence[float], *, size: int) -> list[float]:
    """The most recent window - what a live guard would test."""
    if len(series) < size:
        raise DriftError("thin_series", f"{len(series)} observations for {size}")
    return list(series[-size:])


def guard(
    detection: Detection, allocation: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Wrap an allocation: pass it through when in-distribution, withdraw it when drifted.

    A refused allocation is returned as zeros, not as absent - a sizing layer reading a missing key
    might fall back to a default, and falling back to normal size on drift is the exact failure
    guard exists to prevent.
    """
    if not detection.drifted:
        return {
            "allocated": True,
            "sizes": dict(allocation or {}),
            "detection": detection.to_dict(),
            "reason": "in_distribution",
        }
    return {
        "allocated": False,
        "sizes": {name: 0.0 for name in (allocation or {})},
        "detection": detection.to_dict(),
        "reason": (
            "out_of_distribution: recent outcomes are not from the corpus's world, so reweighting "
            "would treat decay as adaptation; recalibrate before allocating"
        ),
    }


def profile(
    series: Sequence[float], *, size: int, quantile: float = 0.99,
    draws: int = 500, seed: int = 0,
) -> dict[str, Any]:
    """One call: calibrate on the first half, test every window in the second half."""
    if len(series) < size * 4:
        raise DriftError("thin_series", f"{len(series)} observations, need {size * 4}")
    split = len(series) // 2
    reference = list(series[:split])
    threshold = calibrate(reference, size=size, quantile=quantile, draws=draws, seed=seed)
    tested = [
        detect(reference, w, threshold=threshold).to_dict()
        for w in windows(series[split:], size=size)
    ]
    flags = sum(1 for row in tested if row["drifted"])
    return {
        "threshold": round(threshold, 6),
        "reference_size": len(reference),
        "window": size,
        "windows_tested": len(tested),
        "flagged": flags,
        "flag_rate": round(flags / len(tested), 4) if tested else 0.0,
        "worst": max((row["distance"] for row in tested), default=0.0),
        "caveats": [
            "the threshold's false-positive rate is nominal, measured by resampling the reference",
            "out-of-sample flags on a stationary series are expected at roughly (1 - quantile); a "
            "flag rate above that is evidence of drift; far below it, the windows are too long "
            "to see a shift at all",
            "this detects a shift in the outcome distribution, not its cause",
        ],
    }


def render(payload: dict[str, Any]) -> str:
    lines = [
        f"window {payload['window']}  reference {payload['reference_size']}  "
        f"threshold {payload['threshold']:.4f}",
        f"  tested {payload['windows_tested']} windows, flagged {payload['flagged']} "
        f"({payload['flag_rate']:.1%}), worst {payload['worst']:.4f}",
    ]
    for caveat in payload.get("caveats", []):
        lines.append(f"  note: {caveat}")
    return "\n".join(lines)

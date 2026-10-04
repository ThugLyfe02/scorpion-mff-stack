from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DriftSignal:
    observations: int
    mean: float
    statistic: float
    drifted: bool


class PageHinkley:
    """One-sided online drift detector for upward degradation metrics."""

    def __init__(
        self,
        *,
        delta: float = 0.005,
        threshold: float = 5.0,
        min_instances: int = 30,
        reset_on_drift: bool = False,
    ) -> None:
        if delta < 0.0 or threshold <= 0.0 or min_instances <= 0:
            raise ValueError("invalid Page-Hinkley parameters")
        self.delta = delta
        self.threshold = threshold
        self.min_instances = min_instances
        self.reset_on_drift = reset_on_drift
        self.reset()

    def reset(self) -> None:
        self._observations = 0
        self._mean = 0.0
        self._cumulative = 0.0
        self._minimum = 0.0

    def update(self, value: float) -> DriftSignal:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        self._observations += 1
        self._mean += (value - self._mean) / self._observations
        self._cumulative += value - self._mean - self.delta
        self._minimum = min(self._minimum, self._cumulative)
        statistic = self._cumulative - self._minimum
        drifted = self._observations >= self.min_instances and statistic >= self.threshold
        signal = DriftSignal(self._observations, self._mean, statistic, drifted)
        if drifted and self.reset_on_drift:
            self.reset()
        return signal

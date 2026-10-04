from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .resilience import OperationalMode


@dataclass(frozen=True, slots=True)
class TimedOutcome:
    ts_utc: datetime
    bad: bool

    def __post_init__(self) -> None:
        if self.ts_utc.tzinfo is None or self.ts_utc.utcoffset() is None:
            raise ValueError("ts_utc must be timezone-aware")
        object.__setattr__(self, "ts_utc", self.ts_utc.astimezone(UTC))


class BurnSeverity(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True, slots=True)
class SLOBudget:
    target_bad_rate: float = 0.01
    short_window: timedelta = timedelta(minutes=5)
    long_window: timedelta = timedelta(hours=1)
    degraded_burn_rate: float = 2.0
    halted_burn_rate: float = 8.0
    min_short_samples: int = 20
    min_long_samples: int = 100

    def __post_init__(self) -> None:
        if not 0.0 < self.target_bad_rate < 1.0:
            raise ValueError("target_bad_rate must be in (0,1)")
        if self.short_window <= timedelta(0) or self.long_window <= self.short_window:
            raise ValueError("SLO windows must be positive and long > short")
        if not 0.0 < self.degraded_burn_rate < self.halted_burn_rate:
            raise ValueError("burn thresholds must satisfy 0 < degraded < halted")
        if self.min_short_samples <= 0 or self.min_long_samples <= 0:
            raise ValueError("minimum sample counts must be positive")


@dataclass(frozen=True, slots=True)
class BurnRateReport:
    short_samples: int
    long_samples: int
    short_bad_rate: float
    long_bad_rate: float
    short_burn_rate: float
    long_burn_rate: float
    severity: BurnSeverity


def _window_rate(
    outcomes: tuple[TimedOutcome, ...],
    *,
    now: datetime,
    window: timedelta,
) -> tuple[int, float]:
    cutoff = now - window
    rows = [item for item in outcomes if cutoff <= item.ts_utc <= now]
    if not rows:
        return 0, 0.0
    return len(rows), sum(item.bad for item in rows) / len(rows)


def evaluate_burn_rate(
    outcomes: tuple[TimedOutcome, ...],
    *,
    budget: SLOBudget | None = None,
    now: datetime | None = None,
) -> BurnRateReport:
    budget = budget or SLOBudget()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    short_samples, short_rate = _window_rate(
        outcomes,
        now=now,
        window=budget.short_window,
    )
    long_samples, long_rate = _window_rate(
        outcomes,
        now=now,
        window=budget.long_window,
    )
    short_burn = short_rate / budget.target_bad_rate
    long_burn = long_rate / budget.target_bad_rate
    if short_samples < budget.min_short_samples or long_samples < budget.min_long_samples:
        severity = BurnSeverity.INSUFFICIENT_DATA
    elif (
        short_burn >= budget.halted_burn_rate
        and long_burn >= budget.degraded_burn_rate
    ):
        severity = BurnSeverity.HALTED
    elif (
        short_burn >= budget.degraded_burn_rate
        and long_burn >= 1.0
    ):
        severity = BurnSeverity.DEGRADED
    else:
        severity = BurnSeverity.OK
    return BurnRateReport(
        short_samples=short_samples,
        long_samples=long_samples,
        short_bad_rate=short_rate,
        long_bad_rate=long_rate,
        short_burn_rate=short_burn,
        long_burn_rate=long_burn,
        severity=severity,
    )


class ModeHysteresis:
    """Escalate immediately; require sustained health before de-escalating mode."""

    _RANK = {
        OperationalMode.NORMAL: 0,
        OperationalMode.DEGRADED: 1,
        OperationalMode.HALTED: 2,
    }

    def __init__(self, *, recovery_confirmations: int = 3) -> None:
        if recovery_confirmations <= 0:
            raise ValueError("recovery_confirmations must be positive")
        self.recovery_confirmations = recovery_confirmations
        self.mode = OperationalMode.NORMAL
        self._healthy_confirmations = 0

    def update(self, proposed: OperationalMode) -> OperationalMode:
        current_rank = self._RANK[self.mode]
        proposed_rank = self._RANK[proposed]
        if proposed_rank > current_rank:
            self.mode = proposed
            self._healthy_confirmations = 0
            return self.mode
        if proposed_rank == current_rank:
            self._healthy_confirmations = 0
            return self.mode

        self._healthy_confirmations += 1
        if self._healthy_confirmations >= self.recovery_confirmations:
            self.mode = proposed
            self._healthy_confirmations = 0
        return self.mode

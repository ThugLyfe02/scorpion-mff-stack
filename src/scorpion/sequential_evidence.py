from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class SequentialEvidenceStatus(StrEnum):
    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    ALARM = "ALARM"


@dataclass(frozen=True, slots=True)
class BernoulliEPolicy:
    """Anytime-valid sequential test for an excessive Bernoulli violation rate.

    The null is ``p <= null_rate`` and the fixed alternative is ``p=alternative_rate`` with
    ``alternative_rate > null_rate``. The likelihood-ratio process is a nonnegative
    supermartingale under the composite null, so Ville's inequality permits continuous
    monitoring without the repeated-peeking problem of ordinary fixed-sample p-values.
    """

    null_rate: float
    alternative_rate: float
    alpha: float = 0.01
    watch_e_value: float = 10.0

    def __post_init__(self) -> None:
        if not 0 < self.null_rate < 1:
            raise ValueError("null_rate must be in (0,1)")
        if not self.null_rate < self.alternative_rate < 1:
            raise ValueError("alternative_rate must be in (null_rate,1)")
        if not 0 < self.alpha < 1:
            raise ValueError("alpha must be in (0,1)")
        if self.watch_e_value <= 1:
            raise ValueError("watch_e_value must be >1")


@dataclass(frozen=True, slots=True)
class SequentialEvidencePoint:
    observations: int
    violations: int
    empirical_rate: float
    log_e_value: float
    e_value: float
    max_e_value: float
    anytime_p_value: float
    status: SequentialEvidenceStatus


class BernoulliEProcess:
    def __init__(self, policy: BernoulliEPolicy) -> None:
        self.policy = policy
        self.observations = 0
        self.violations = 0
        self.log_e_value = 0.0
        self.max_log_e_value = 0.0
        self._log_hit = math.log(policy.alternative_rate / policy.null_rate)
        self._log_miss = math.log(
            (1.0 - policy.alternative_rate) / (1.0 - policy.null_rate)
        )

    def observe(self, violation: bool) -> SequentialEvidencePoint:
        self.observations += 1
        self.violations += int(violation)
        self.log_e_value += self._log_hit if violation else self._log_miss
        self.max_log_e_value = max(self.max_log_e_value, self.log_e_value)
        return self.snapshot()

    def snapshot(self) -> SequentialEvidencePoint:
        e_value = _safe_exp(self.log_e_value)
        max_e = _safe_exp(self.max_log_e_value)
        alarm_threshold = 1.0 / self.policy.alpha
        if max_e >= alarm_threshold:
            status = SequentialEvidenceStatus.ALARM
        elif max_e >= self.policy.watch_e_value:
            status = SequentialEvidenceStatus.WATCH
        else:
            status = SequentialEvidenceStatus.HEALTHY
        return SequentialEvidencePoint(
            observations=self.observations,
            violations=self.violations,
            empirical_rate=(
                self.violations / self.observations if self.observations else 0.0
            ),
            log_e_value=self.log_e_value,
            e_value=e_value,
            max_e_value=max_e,
            anytime_p_value=min(1.0, 1.0 / max_e if max_e > 0 else 1.0),
            status=status,
        )


def _safe_exp(value: float) -> float:
    if value >= 709:
        return math.inf
    if value <= -745:
        return 0.0
    return math.exp(value)


@dataclass(frozen=True, slots=True)
class ExecutionEvidenceMonitorPolicy:
    fill_bound: BernoulliEPolicy = BernoulliEPolicy(
        null_rate=0.01,
        alternative_rate=0.05,
        alpha=0.01,
    )
    actionable_model_error: BernoulliEPolicy = BernoulliEPolicy(
        null_rate=0.005,
        alternative_rate=0.03,
        alpha=0.01,
    )


@dataclass(frozen=True, slots=True)
class ExecutionEvidenceMonitorSnapshot:
    fill_bound: SequentialEvidencePoint
    actionable_model_error: SequentialEvidencePoint
    status: SequentialEvidenceStatus


class ExecutionEvidenceMonitor:
    """Continuously monitor fill-bound and actionable-model failures in shadow/paper mode."""

    def __init__(self, policy: ExecutionEvidenceMonitorPolicy | None = None) -> None:
        policy = policy or ExecutionEvidenceMonitorPolicy()
        self.fill_bound = BernoulliEProcess(policy.fill_bound)
        self.actionable_model_error = BernoulliEProcess(policy.actionable_model_error)

    def observe_fill_bound(self, *, violated: bool) -> ExecutionEvidenceMonitorSnapshot:
        self.fill_bound.observe(violated)
        return self.snapshot()

    def observe_actionable_model_error(
        self,
        *,
        wrong_actionable_singleton: bool,
    ) -> ExecutionEvidenceMonitorSnapshot:
        self.actionable_model_error.observe(wrong_actionable_singleton)
        return self.snapshot()

    def snapshot(self) -> ExecutionEvidenceMonitorSnapshot:
        fill = self.fill_bound.snapshot()
        model = self.actionable_model_error.snapshot()
        statuses = {fill.status, model.status}
        if SequentialEvidenceStatus.ALARM in statuses:
            status = SequentialEvidenceStatus.ALARM
        elif SequentialEvidenceStatus.WATCH in statuses:
            status = SequentialEvidenceStatus.WATCH
        else:
            status = SequentialEvidenceStatus.HEALTHY
        return ExecutionEvidenceMonitorSnapshot(fill, model, status)

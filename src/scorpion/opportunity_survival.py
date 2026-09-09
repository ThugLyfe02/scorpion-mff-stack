from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class OpportunitySurvivalStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class OpportunityObservation:
    event_id: str
    group: str
    duration_ms: float
    opportunity_lost: bool

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.group.strip():
            raise ValueError("event_id and group are required")
        if self.duration_ms < 0:
            raise ValueError("duration_ms cannot be negative")


@dataclass(frozen=True, slots=True)
class OpportunitySurvivalPolicy:
    minimum_samples: int = 30
    horizons_ms: tuple[float, ...] = (50.0, 100.0, 250.0, 500.0, 1000.0, 2000.0)
    restricted_mean_horizon_ms: float = 5000.0

    def __post_init__(self) -> None:
        if self.minimum_samples <= 0:
            raise ValueError("minimum_samples must be positive")
        if not self.horizons_ms or any(value < 0 for value in self.horizons_ms):
            raise ValueError("horizons_ms must be non-empty and non-negative")
        if self.restricted_mean_horizon_ms <= 0:
            raise ValueError("restricted_mean_horizon_ms must be positive")


@dataclass(frozen=True, slots=True)
class SurvivalPoint:
    time_ms: float
    at_risk: int
    events: int
    censored: int
    survival: float


@dataclass(frozen=True, slots=True)
class GroupOpportunitySurvival:
    group: str
    samples: int
    observed_losses: int
    censored: int
    censor_rate: float
    median_survival_ms: float | None
    restricted_mean_survival_ms: float
    survival_at_horizons: tuple[tuple[float, float], ...]
    curve: tuple[SurvivalPoint, ...]
    status: OpportunitySurvivalStatus

    @property
    def qualified(self) -> bool:
        return self.status is OpportunitySurvivalStatus.QUALIFIED


@dataclass(frozen=True, slots=True)
class OpportunitySurvivalReport:
    groups: tuple[GroupOpportunitySurvival, ...]
    shortest_median_group: str | None
    shortest_median_ms: float | None


def _kaplan_meier(rows: Sequence[OpportunityObservation]) -> tuple[SurvivalPoint, ...]:
    timeline: dict[float, list[OpportunityObservation]] = defaultdict(list)
    for row in rows:
        timeline[row.duration_ms].append(row)
    at_risk = len(rows)
    survival = 1.0
    points: list[SurvivalPoint] = []
    for time_ms in sorted(timeline):
        bucket = timeline[time_ms]
        events = sum(item.opportunity_lost for item in bucket)
        censored = len(bucket) - events
        if at_risk > 0 and events:
            survival *= 1.0 - events / at_risk
        points.append(
            SurvivalPoint(
                time_ms=time_ms,
                at_risk=at_risk,
                events=events,
                censored=censored,
                survival=max(0.0, survival),
            )
        )
        at_risk -= len(bucket)
    return tuple(points)


def _survival_at(curve: Sequence[SurvivalPoint], horizon_ms: float) -> float:
    survival = 1.0
    for point in curve:
        if point.time_ms > horizon_ms:
            break
        survival = point.survival
    return survival


def _median(curve: Sequence[SurvivalPoint]) -> float | None:
    for point in curve:
        if point.survival <= 0.5:
            return point.time_ms
    return None


def _restricted_mean(curve: Sequence[SurvivalPoint], horizon_ms: float) -> float:
    if horizon_ms <= 0:
        return 0.0
    area = 0.0
    previous_time = 0.0
    previous_survival = 1.0
    for point in curve:
        current = min(point.time_ms, horizon_ms)
        if current > previous_time:
            area += (current - previous_time) * previous_survival
        if point.time_ms >= horizon_ms:
            return area
        previous_time = point.time_ms
        previous_survival = point.survival
    if previous_time < horizon_ms:
        area += (horizon_ms - previous_time) * previous_survival
    return area


def evaluate_opportunity_survival(
    observations: Sequence[OpportunityObservation],
    *,
    policy: OpportunitySurvivalPolicy | None = None,
) -> OpportunitySurvivalReport:
    """Estimate how long immediate execution opportunity survives after an alert.

    Censored rows mean the opportunity was still alive when the observation window ended. The
    Kaplan-Meier estimator therefore does not incorrectly treat every observation window end as
    an opportunity failure.
    """
    policy = policy or OpportunitySurvivalPolicy()
    grouped: dict[str, list[OpportunityObservation]] = defaultdict(list)
    for row in observations:
        grouped[row.group].append(row)

    reports: list[GroupOpportunitySurvival] = []
    for group in sorted(grouped):
        rows = grouped[group]
        curve = _kaplan_meier(rows)
        losses = sum(item.opportunity_lost for item in rows)
        censored = len(rows) - losses
        status = (
            OpportunitySurvivalStatus.QUALIFIED
            if len(rows) >= policy.minimum_samples
            else OpportunitySurvivalStatus.INSUFFICIENT
        )
        reports.append(
            GroupOpportunitySurvival(
                group=group,
                samples=len(rows),
                observed_losses=losses,
                censored=censored,
                censor_rate=censored / len(rows) if rows else 0.0,
                median_survival_ms=_median(curve),
                restricted_mean_survival_ms=_restricted_mean(
                    curve,
                    policy.restricted_mean_horizon_ms,
                ),
                survival_at_horizons=tuple(
                    (horizon, _survival_at(curve, horizon))
                    for horizon in policy.horizons_ms
                ),
                curve=curve,
                status=status,
            )
        )

    finite = [item for item in reports if item.median_survival_ms is not None]
    if finite:
        shortest = min(finite, key=lambda item: float(item.median_survival_ms or math.inf))
        shortest_group = shortest.group
        shortest_median = shortest.median_survival_ms
    else:
        shortest_group = None
        shortest_median = None
    return OpportunitySurvivalReport(tuple(reports), shortest_group, shortest_median)

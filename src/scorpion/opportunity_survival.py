from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from .domain import EventKind
from .microstructure import (
    MarketEventKind,
    OptionMicrostructureTape,
    ns_from_datetime,
    timedelta_to_ns,
)
from .microstructure_forensics import MicrostructureForensicsReport


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
class OpportunityExtractionPolicy:
    observation_horizon: timedelta = timedelta(seconds=5)
    market_quote_max_age: timedelta = timedelta(seconds=1)
    include_adds: bool = True

    def __post_init__(self) -> None:
        if self.observation_horizon <= timedelta(0):
            raise ValueError("observation_horizon must be positive")
        if self.market_quote_max_age < timedelta(0):
            raise ValueError("market_quote_max_age cannot be negative")


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


def extract_opportunity_observations(
    report: MicrostructureForensicsReport,
    tape: OptionMicrostructureTape,
    *,
    policy: OpportunityExtractionPolicy | None = None,
) -> tuple[OpportunityObservation, ...]:
    """Measure first loss of immediate marketability from the real forensic limit path."""
    policy = policy or OpportunityExtractionPolicy()
    allowed_kinds = {EventKind.ENTRY}
    if policy.include_adds:
        allowed_kinds.add(EventKind.ADD)
    horizon_ns = timedelta_to_ns(policy.observation_horizon)
    observations: list[OpportunityObservation] = []
    for leg in report.legs:
        if leg.event_kind not in allowed_kinds:
            continue
        if leg.contract_key is None or leg.limit_price is None or leg.order_arrival_ts_utc is None:
            continue
        arrival_ns = ns_from_datetime(leg.order_arrival_ts_utc)
        market = tape.market_quote_at(
            leg.contract_key,
            arrival_ns,
            max_age=policy.market_quote_max_age,
        )
        if market is None or market.ask is None:
            continue
        group = leg.bucket.value
        if market.ask > leg.limit_price:
            observations.append(OpportunityObservation(leg.event_id, group, 0.0, True))
            continue
        deadline_ns = arrival_ns + horizon_ns
        loss_ns: int | None = None
        for event in tape.events_between(leg.contract_key, arrival_ns, deadline_ns):
            if (
                event.kind is MarketEventKind.QUOTE
                and event.ask is not None
                and event.ask > leg.limit_price
            ):
                loss_ns = event.ts_event_ns
                break
        if loss_ns is None:
            observations.append(
                OpportunityObservation(
                    leg.event_id,
                    group,
                    horizon_ns / 1_000_000.0,
                    False,
                )
            )
        else:
            observations.append(
                OpportunityObservation(
                    leg.event_id,
                    group,
                    max(0.0, (loss_ns - arrival_ns) / 1_000_000.0),
                    True,
                )
            )
    return tuple(observations)


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

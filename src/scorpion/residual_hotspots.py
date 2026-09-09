from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import combinations

from .domain import EventKind


class ResidualHotspotStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class ResidualModelObservation:
    event_id: str
    model_id: str
    truth: EventKind
    predicted: EventKind
    slices: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.model_id.strip():
            raise ValueError("event_id and model_id are required")
        if not self.slices:
            raise ValueError("slices cannot be empty")
        if any(not key.strip() or not value.strip() for key, value in self.slices.items()):
            raise ValueError("slice names and values must be non-empty")


@dataclass(frozen=True, slots=True)
class ResidualHotspotPolicy:
    minimum_models: int = 2
    minimum_events: int = 100
    minimum_slice_events: int = 20
    minimum_complement_events: int = 20
    consensus_failure_fraction: float = 0.50
    minimum_absolute_failure_lift: float = 0.10
    maximum_fdr_q: float = 0.05
    maximum_robust_hotspots: int = 0
    interaction_order: int = 2
    maximum_values_per_dimension: int = 20

    def __post_init__(self) -> None:
        if self.minimum_models < 2:
            raise ValueError("minimum_models must be >=2")
        if self.minimum_events <= 0:
            raise ValueError("minimum_events must be positive")
        if self.minimum_slice_events <= 0 or self.minimum_complement_events <= 0:
            raise ValueError("slice/complement thresholds must be positive")
        for name in (
            "consensus_failure_fraction",
            "minimum_absolute_failure_lift",
            "maximum_fdr_q",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        if self.maximum_robust_hotspots < 0:
            raise ValueError("maximum_robust_hotspots cannot be negative")
        if self.interaction_order not in (1, 2):
            raise ValueError("interaction_order must be 1 or 2")
        if self.maximum_values_per_dimension <= 0:
            raise ValueError("maximum_values_per_dimension must be positive")


@dataclass(frozen=True, slots=True)
class ResidualSliceEvidence:
    slice_key: str
    events: int
    failures: int
    failure_rate: float
    complement_events: int
    complement_failures: int
    complement_failure_rate: float
    absolute_lift: float
    p_value: float
    q_value: float
    robust_hotspot: bool
    priority_score: float


@dataclass(frozen=True, slots=True)
class ResidualHotspotReport:
    models: tuple[str, ...]
    events: int
    global_failure_rate: float
    tested_slices: int
    robust_hotspots: tuple[ResidualSliceEvidence, ...]
    evidence: tuple[ResidualSliceEvidence, ...]
    status: ResidualHotspotStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is ResidualHotspotStatus.QUALIFIED


@dataclass(frozen=True, slots=True)
class _EventResidual:
    event_id: str
    failed: bool
    slices: tuple[tuple[str, str], ...]


def _event_rows(
    rows: Sequence[ResidualModelObservation],
    policy: ResidualHotspotPolicy,
) -> tuple[tuple[str, ...], tuple[_EventResidual, ...]]:
    grouped: dict[str, list[ResidualModelObservation]] = defaultdict(list)
    for row in rows:
        grouped[row.event_id].append(row)
    models = tuple(sorted({row.model_id for row in rows}))
    expected_models = set(models)
    output: list[_EventResidual] = []
    for event_id in sorted(grouped):
        event_rows = grouped[event_id]
        if {row.model_id for row in event_rows} != expected_models:
            raise ValueError(f"event {event_id} does not contain the full model set")
        truths = {row.truth for row in event_rows}
        if len(truths) != 1:
            raise ValueError(f"truth mismatch for event {event_id}")
        canonical_slices = tuple(sorted(event_rows[0].slices.items()))
        if any(tuple(sorted(row.slices.items())) != canonical_slices for row in event_rows[1:]):
            raise ValueError(f"slice mismatch for event {event_id}")
        wrong = sum(row.predicted is not row.truth for row in event_rows)
        output.append(
            _EventResidual(
                event_id=event_id,
                failed=wrong / len(event_rows) >= policy.consensus_failure_fraction,
                slices=canonical_slices,
            )
        )
    return models, tuple(output)


def _eligible_dimensions(
    events: Sequence[_EventResidual],
    policy: ResidualHotspotPolicy,
) -> tuple[str, ...]:
    values: dict[str, set[str]] = defaultdict(set)
    for event in events:
        for key, value in event.slices:
            values[key].add(value)
    return tuple(
        sorted(
            key
            for key, members in values.items()
            if len(members) <= policy.maximum_values_per_dimension
        )
    )


def _slice_keys(
    event: _EventResidual,
    dimensions: tuple[str, ...],
    interaction_order: int,
) -> tuple[str, ...]:
    values = dict(event.slices)
    singles = tuple(f"{key}={values[key]}" for key in dimensions if key in values)
    if interaction_order < 2:
        return singles
    pairs = tuple(
        f"{left}={values[left]}&{right}={values[right]}"
        for left, right in combinations(dimensions, 2)
        if left in values and right in values
    )
    return singles + pairs


def _one_sided_two_proportion_p(
    failures: int,
    events: int,
    complement_failures: int,
    complement_events: int,
) -> float:
    if events <= 0 or complement_events <= 0:
        return 1.0
    left_rate = failures / events
    right_rate = complement_failures / complement_events
    pooled = (failures + complement_failures) / (events + complement_events)
    variance = pooled * (1.0 - pooled) * (1.0 / events + 1.0 / complement_events)
    if variance <= 0:
        return 0.0 if left_rate > right_rate else 1.0
    z_value = (left_rate - right_rate) / math.sqrt(variance)
    return 0.5 * math.erfc(z_value / math.sqrt(2.0))


def _bh_q_values(p_values: Sequence[float]) -> tuple[float, ...]:
    if not p_values:
        return ()
    indexed = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [1.0] * len(p_values)
    running = 1.0
    total = len(p_values)
    for reverse_rank, (index, p_value) in enumerate(reversed(indexed), start=1):
        rank = total - reverse_rank + 1
        candidate = min(1.0, p_value * total / rank)
        running = min(running, candidate)
        adjusted[index] = running
    return tuple(adjusted)


def evaluate_residual_hotspots(
    rows: Sequence[ResidualModelObservation],
    *,
    policy: ResidualHotspotPolicy | None = None,
) -> ResidualHotspotReport:
    """Find FDR-controlled causal slices where multiple models fail together.

    Slice values must be point-in-time features supplied by the caller. This function never uses
    future outcomes as features and never mutates a model; it only identifies targeted research
    hotspots for review/challenger generation.
    """
    policy = policy or ResidualHotspotPolicy()
    models, events = _event_rows(rows, policy)
    failures: list[str] = []
    if len(models) < policy.minimum_models:
        failures.append(f"insufficient_models:{len(models)}<{policy.minimum_models}")
    if len(events) < policy.minimum_events:
        failures.append(f"insufficient_events:{len(events)}<{policy.minimum_events}")
    global_rate = sum(event.failed for event in events) / len(events) if events else 0.0
    if failures:
        return ResidualHotspotReport(
            models=models,
            events=len(events),
            global_failure_rate=global_rate,
            tested_slices=0,
            robust_hotspots=(),
            evidence=(),
            status=ResidualHotspotStatus.INSUFFICIENT,
            failures=tuple(failures),
        )

    dimensions = _eligible_dimensions(events, policy)
    members: dict[str, list[_EventResidual]] = defaultdict(list)
    for event in events:
        for key in _slice_keys(event, dimensions, policy.interaction_order):
            members[key].append(event)

    prelim: list[tuple[str, int, int, int, int, float]] = []
    total_failures = sum(event.failed for event in events)
    for slice_key in sorted(members):
        slice_events = members[slice_key]
        event_count = len(slice_events)
        complement_count = len(events) - event_count
        if (
            event_count < policy.minimum_slice_events
            or complement_count < policy.minimum_complement_events
        ):
            continue
        slice_failures = sum(event.failed for event in slice_events)
        complement_failures = total_failures - slice_failures
        p_value = _one_sided_two_proportion_p(
            slice_failures,
            event_count,
            complement_failures,
            complement_count,
        )
        prelim.append(
            (
                slice_key,
                event_count,
                slice_failures,
                complement_count,
                complement_failures,
                p_value,
            )
        )
    q_values = _bh_q_values([item[5] for item in prelim])
    evidence: list[ResidualSliceEvidence] = []
    for row, q_value in zip(prelim, q_values, strict=True):
        (
            slice_key,
            event_count,
            slice_failures,
            complement_count,
            complement_failures,
            p_value,
        ) = row
        failure_rate = slice_failures / event_count
        complement_rate = complement_failures / complement_count
        lift = failure_rate - complement_rate
        robust = (
            lift >= policy.minimum_absolute_failure_lift
            and q_value <= policy.maximum_fdr_q
        )
        priority = max(0.0, lift) * math.sqrt(event_count) * (1.0 - min(1.0, q_value))
        evidence.append(
            ResidualSliceEvidence(
                slice_key=slice_key,
                events=event_count,
                failures=slice_failures,
                failure_rate=failure_rate,
                complement_events=complement_count,
                complement_failures=complement_failures,
                complement_failure_rate=complement_rate,
                absolute_lift=lift,
                p_value=p_value,
                q_value=q_value,
                robust_hotspot=robust,
                priority_score=priority,
            )
        )
    evidence.sort(key=lambda item: (-item.priority_score, item.slice_key))
    hotspots = tuple(item for item in evidence if item.robust_hotspot)
    if len(hotspots) > policy.maximum_robust_hotspots:
        failures.append(
            f"robust_residual_hotspots:{len(hotspots)}>{policy.maximum_robust_hotspots}"
        )
    status = ResidualHotspotStatus.QUALIFIED if not failures else ResidualHotspotStatus.FAILED
    return ResidualHotspotReport(
        models=models,
        events=len(events),
        global_failure_rate=global_rate,
        tested_slices=len(evidence),
        robust_hotspots=hotspots,
        evidence=tuple(evidence),
        status=status,
        failures=tuple(failures),
    )

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from .calibration import CalibrationStatus
from .domain import EventKind
from .resilience import OperationalMode


class InferenceDepth(StrEnum):
    NONE = "NONE"
    LIGHT = "LIGHT"
    DEEP = "DEEP"


class InferencePriority(IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass(frozen=True, slots=True)
class InferenceContext:
    event_id: str
    event_kind: EventKind
    parser_confidence: float
    association_confidence: float
    novelty_score: float
    ensemble_entropy: float
    calibration_status: CalibrationStatus
    operational_mode: OperationalMode
    parser_conflict: bool = False
    source_shift_score: float = 0.0


@dataclass(frozen=True, slots=True)
class InferenceRoute:
    event_id: str
    depth: InferenceDepth
    priority: InferencePriority
    cost_units: int
    information_score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InferenceAllocation:
    selected: tuple[InferenceRoute, ...]
    deferred: tuple[InferenceRoute, ...]
    budget_units: int
    used_units: int


def route_inference(context: InferenceContext) -> InferenceRoute:
    values = (
        context.parser_confidence,
        context.association_confidence,
        context.novelty_score,
        context.ensemble_entropy,
    )
    if any(not 0.0 <= value <= 1.0 for value in values):
        raise ValueError("confidence/novelty/entropy values must be between 0 and 1")
    if context.source_shift_score < 0:
        raise ValueError("source_shift_score cannot be negative")

    score = 0.0
    reasons: list[str] = []
    score += (1.0 - context.parser_confidence) * 25.0
    score += (1.0 - context.association_confidence) * 20.0
    score += context.novelty_score * 25.0
    score += context.ensemble_entropy * 15.0
    score += min(10.0, context.source_shift_score * 5.0)

    if context.event_kind is EventKind.AMBIGUOUS:
        score += 25.0
        reasons.append("ambiguous_deterministic_parse")
    if context.parser_conflict:
        score += 20.0
        reasons.append("parser_conflict")
    if context.calibration_status in {
        CalibrationStatus.UNCALIBRATED,
        CalibrationStatus.DEGRADED,
    }:
        score += 15.0
        reasons.append("calibration_not_trusted")
    elif context.calibration_status is CalibrationStatus.PROVISIONAL:
        score += 7.5
        reasons.append("calibration_provisional")
    if context.operational_mode is OperationalMode.DEGRADED:
        score += 10.0
        reasons.append("system_degraded")
    elif context.operational_mode is OperationalMode.HALTED:
        score += 20.0
        reasons.append("system_halted_diagnostic_only")

    score = min(100.0, score)
    if score >= 65.0:
        depth = InferenceDepth.DEEP
        priority = InferencePriority.CRITICAL if score >= 85.0 else InferencePriority.HIGH
        cost = 5
    elif score >= 30.0:
        depth = InferenceDepth.LIGHT
        priority = InferencePriority.NORMAL if score < 50.0 else InferencePriority.HIGH
        cost = 1
    else:
        depth = InferenceDepth.NONE
        priority = InferencePriority.LOW
        cost = 0
        reasons.append("deterministic_evidence_sufficient")

    if context.novelty_score >= 0.70:
        reasons.append("novel_wording")
    if context.ensemble_entropy >= 0.30:
        reasons.append("ensemble_uncertainty")
    if context.association_confidence < 0.75:
        reasons.append("weak_association")
    return InferenceRoute(
        context.event_id,
        depth,
        priority,
        cost,
        score,
        tuple(dict.fromkeys(reasons)),
    )


def allocate_inference_budget(
    contexts: Sequence[InferenceContext],
    *,
    budget_units: int,
) -> InferenceAllocation:
    """Spend shadow-model compute where marginal information value is highest.

    This only allocates observation/research compute. It cannot change deterministic state or
    create execution effects.
    """
    if budget_units < 0:
        raise ValueError("budget_units cannot be negative")
    routes = [route_inference(context) for context in contexts]
    routes.sort(
        key=lambda route: (
            -int(route.priority),
            -route.information_score,
            route.cost_units,
            route.event_id,
        )
    )
    selected: list[InferenceRoute] = []
    deferred: list[InferenceRoute] = []
    used = 0
    for route in routes:
        if route.cost_units == 0:
            selected.append(route)
            continue
        if used + route.cost_units <= budget_units:
            selected.append(route)
            used += route.cost_units
        else:
            deferred.append(route)
    return InferenceAllocation(tuple(selected), tuple(deferred), budget_units, used)

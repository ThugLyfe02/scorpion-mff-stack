from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .sizing_lab import SegmentMetrics, SizingReadiness


class SelectionStatus(StrEnum):
    SELECTED = "SELECTED"
    REJECTED_SAMPLE = "REJECTED_SAMPLE"
    REJECTED_EDGE = "REJECTED_EDGE"
    REJECTED_MULTIPLE_TESTING = "REJECTED_MULTIPLE_TESTING"
    REJECTED_STABILITY = "REJECTED_STABILITY"
    REJECTED_DRAWDOWN = "REJECTED_DRAWDOWN"
    REJECTED_WIN_FLOOR = "REJECTED_WIN_FLOOR"


@dataclass(frozen=True, slots=True)
class StrategySelectionPolicy:
    minimum_samples: int = 30
    maximum_fdr_q_value: float = 0.10
    minimum_win_rate_lower_90: float = 0.45
    minimum_positive_fold_ratio: float = 0.75
    maximum_observed_drawdown: float = 0.50
    maximum_candidates: int = 10


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    segment: str
    status: SelectionStatus
    score: float
    reason: str


def evaluate_candidate(
    metric: SegmentMetrics,
    *,
    policy: StrategySelectionPolicy | None = None,
) -> StrategyCandidate:
    policy = policy or StrategySelectionPolicy()
    if metric.samples < policy.minimum_samples:
        return StrategyCandidate(
            metric.segment,
            SelectionStatus.REJECTED_SAMPLE,
            0.0,
            f"samples={metric.samples} < {policy.minimum_samples}",
        )
    if metric.readiness is SizingReadiness.MULTIPLE_TESTING_NOT_SIGNIFICANT:
        return StrategyCandidate(
            metric.segment,
            SelectionStatus.REJECTED_MULTIPLE_TESTING,
            0.0,
            f"fdr_q_value={metric.fdr_q_value:.4f}",
        )
    if metric.fdr_q_value > policy.maximum_fdr_q_value:
        return StrategyCandidate(
            metric.segment,
            SelectionStatus.REJECTED_MULTIPLE_TESTING,
            0.0,
            f"fdr_q_value={metric.fdr_q_value:.4f}",
        )
    if (
        metric.readiness is not SizingReadiness.READY_FOR_RESEARCH
        or metric.conservative_edge <= 0
    ):
        return StrategyCandidate(
            metric.segment,
            SelectionStatus.REJECTED_EDGE,
            0.0,
            f"conservative_edge={metric.conservative_edge:.4f}",
        )
    if metric.positive_fold_ratio < policy.minimum_positive_fold_ratio:
        return StrategyCandidate(
            metric.segment,
            SelectionStatus.REJECTED_STABILITY,
            0.0,
            f"positive_fold_ratio={metric.positive_fold_ratio:.3f}",
        )
    if metric.max_drawdown > policy.maximum_observed_drawdown:
        return StrategyCandidate(
            metric.segment,
            SelectionStatus.REJECTED_DRAWDOWN,
            0.0,
            f"max_drawdown={metric.max_drawdown:.3f}",
        )
    if metric.win_rate_lower_90 < policy.minimum_win_rate_lower_90:
        return StrategyCandidate(
            metric.segment,
            SelectionStatus.REJECTED_WIN_FLOOR,
            0.0,
            f"win_rate_lower_90={metric.win_rate_lower_90:.3f}",
        )
    return StrategyCandidate(
        metric.segment,
        SelectionStatus.SELECTED,
        metric.edge_score,
        (
            "passes sample depth, false-discovery control, lower-bound edge, time-fold "
            "stability, drawdown, and win-floor gates; research candidate only, not a "
            "profit guarantee"
        ),
    )


def select_candidates(
    metrics: tuple[SegmentMetrics, ...],
    *,
    policy: StrategySelectionPolicy | None = None,
) -> tuple[StrategyCandidate, ...]:
    policy = policy or StrategySelectionPolicy()
    evaluated = tuple(evaluate_candidate(metric, policy=policy) for metric in metrics)
    selected = sorted(
        (item for item in evaluated if item.status is SelectionStatus.SELECTED),
        key=lambda item: item.score,
        reverse=True,
    )[: policy.maximum_candidates]
    rejected = [item for item in evaluated if item.status is not SelectionStatus.SELECTED]
    return tuple(selected + rejected)

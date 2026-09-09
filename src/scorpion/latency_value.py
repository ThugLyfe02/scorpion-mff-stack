from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from .history_archive import HistoryArchive
from .microstructure import OptionMicrostructureTape
from .microstructure_forensics import (
    MicrostructureExecutionProfile,
    run_archive_microstructure_forensics,
)
from .policy_bundle import RuntimePolicyBundle


class LatencyAxis(StrEnum):
    DECISION = "DECISION"
    ORDER_TRANSPORT = "ORDER_TRANSPORT"
    FEED_TRANSPORT = "FEED_TRANSPORT"


@dataclass(frozen=True, slots=True)
class LatencyScenarioResult:
    axis: LatencyAxis
    latency_ms: int
    certified_actionable_legs: int
    uncertain_actionable_legs: int
    certified_coverage: float
    completed_trades: int
    mean_return: float
    total_pnl: Decimal


@dataclass(frozen=True, slots=True)
class LatencyMarginalValue:
    axis: LatencyAxis
    faster_ms: int
    slower_ms: int
    mean_return_loss: float
    completed_trade_loss: int
    certified_coverage_loss: float
    total_pnl_loss: Decimal


@dataclass(frozen=True, slots=True)
class LatencyValueReport:
    scenarios: tuple[LatencyScenarioResult, ...]
    marginal_values: tuple[LatencyMarginalValue, ...]


def _scenario(
    archive: HistoryArchive,
    tape: OptionMicrostructureTape,
    *,
    axis: LatencyAxis,
    latency_ms: int,
    channel_ids: frozenset[str],
    allowed_author_ids: frozenset[str],
    baseline: MicrostructureExecutionProfile,
    runtime_policy: RuntimePolicyBundle,
    include_research_only: bool,
) -> LatencyScenarioResult:
    if latency_ms < 0:
        raise ValueError("latency_ms cannot be negative")
    from datetime import timedelta

    latency = timedelta(milliseconds=latency_ms)
    if axis is LatencyAxis.DECISION:
        profile = replace(baseline, decision_latency=latency)
    elif axis is LatencyAxis.ORDER_TRANSPORT:
        profile = replace(baseline, order_transport_latency=latency)
    else:
        profile = replace(baseline, feed_transport_latency=latency)

    report = run_archive_microstructure_forensics(
        archive,
        tape,
        channel_ids=channel_ids,
        allowed_author_ids=allowed_author_ids,
        profile=profile,
        runtime_policy=runtime_policy,
        include_research_only=include_research_only,
    )
    denominator = report.certified_actionable_legs + report.uncertain_actionable_legs
    coverage = report.certified_actionable_legs / denominator if denominator else 0.0
    trades = tuple(
        trade for trade in report.completed_trades if trade.depth_evidence_complete
    )
    mean_return = (
        float(sum((trade.return_fraction for trade in trades), Decimal("0")) / len(trades))
        if trades
        else 0.0
    )
    total_pnl = sum((trade.pnl for trade in trades), Decimal("0"))
    return LatencyScenarioResult(
        axis=axis,
        latency_ms=latency_ms,
        certified_actionable_legs=report.certified_actionable_legs,
        uncertain_actionable_legs=report.uncertain_actionable_legs,
        certified_coverage=coverage,
        completed_trades=len(trades),
        mean_return=mean_return,
        total_pnl=total_pnl,
    )


def run_latency_value_frontier(
    archive: HistoryArchive,
    tape: OptionMicrostructureTape,
    *,
    channel_ids: frozenset[str],
    allowed_author_ids: frozenset[str],
    baseline: MicrostructureExecutionProfile | None = None,
    runtime_policy: RuntimePolicyBundle | None = None,
    decision_grid_ms: tuple[int, ...] = (50, 100, 250, 500, 1000),
    order_transport_grid_ms: tuple[int, ...] = (0, 10, 25, 50, 100),
    feed_transport_grid_ms: tuple[int, ...] = (0, 10, 25, 50, 100),
    include_research_only: bool = False,
) -> LatencyValueReport:
    baseline = baseline or MicrostructureExecutionProfile()
    policy = runtime_policy or RuntimePolicyBundle()
    grids = {
        LatencyAxis.DECISION: tuple(sorted(set(decision_grid_ms))),
        LatencyAxis.ORDER_TRANSPORT: tuple(sorted(set(order_transport_grid_ms))),
        LatencyAxis.FEED_TRANSPORT: tuple(sorted(set(feed_transport_grid_ms))),
    }
    scenarios: list[LatencyScenarioResult] = []
    marginals: list[LatencyMarginalValue] = []
    for axis, grid in grids.items():
        if not grid or any(value < 0 for value in grid):
            raise ValueError(f"{axis.value} latency grid must contain non-negative values")
        axis_results = [
            _scenario(
                archive,
                tape,
                axis=axis,
                latency_ms=value,
                channel_ids=channel_ids,
                allowed_author_ids=allowed_author_ids,
                baseline=baseline,
                runtime_policy=policy,
                include_research_only=include_research_only,
            )
            for value in grid
        ]
        scenarios.extend(axis_results)
        for faster, slower in zip(axis_results, axis_results[1:], strict=False):
            marginals.append(
                LatencyMarginalValue(
                    axis=axis,
                    faster_ms=faster.latency_ms,
                    slower_ms=slower.latency_ms,
                    mean_return_loss=faster.mean_return - slower.mean_return,
                    completed_trade_loss=faster.completed_trades - slower.completed_trades,
                    certified_coverage_loss=(
                        faster.certified_coverage - slower.certified_coverage
                    ),
                    total_pnl_loss=faster.total_pnl - slower.total_pnl,
                )
            )
    return LatencyValueReport(tuple(scenarios), tuple(marginals))

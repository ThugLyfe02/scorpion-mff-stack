from __future__ import annotations

from dataclasses import dataclass

from .backtest_overfit import (
    BacktestOverfitReport,
    OverfitPolicy,
    build_daily_return_panel,
    evaluate_backtest_overfit,
)
from .execution_forensics import CompletedTrade
from .sizing_lab import (
    SegmentMetrics,
    SizingConstraints,
    rank_segments,
    segment_completed_trades,
)
from .strategy_overlap import (
    StrategyOverlapPolicy,
    StrategyOverlapReport,
    deduplicate_strategy_universe,
)
from .strategy_selector import StrategyCandidate, StrategySelectionPolicy, select_candidates


@dataclass(frozen=True, slots=True)
class StatisticalUniversePolicy:
    overlap: StrategyOverlapPolicy = StrategyOverlapPolicy()
    sizing: SizingConstraints = SizingConstraints()
    selection: StrategySelectionPolicy = StrategySelectionPolicy()
    overfit: OverfitPolicy = OverfitPolicy()


@dataclass(frozen=True, slots=True)
class StatisticalUniverseReport:
    raw_segments: int
    effective_segments: int
    overlap: StrategyOverlapReport
    raw_segment_names: tuple[str, ...]
    effective_segment_names: tuple[str, ...]
    rankings: tuple[SegmentMetrics, ...]
    candidates: tuple[StrategyCandidate, ...]
    backtest_overfit: BacktestOverfitReport

    @property
    def selection_overfit_ok(self) -> bool:
        return self.backtest_overfit.passed


@dataclass(frozen=True, slots=True)
class StatisticalUniverse:
    segments: dict[str, tuple[CompletedTrade, ...]]
    report: StatisticalUniverseReport


def build_statistical_universe(
    trades: tuple[CompletedTrade, ...],
    *,
    policy: StatisticalUniversePolicy | None = None,
) -> StatisticalUniverse:
    """Create the canonical research universe used for independent strategy selection.

    Raw channel/bucket/ticker segments are generated exactly once, then near-duplicate trade sets
    are collapsed before *any* multiple-testing-aware ranking, candidate selection, or CSCV/PBO
    analysis. This prevents parent/child aliases of the same trades from inflating search breadth.
    The raw overlap graph remains part of the report so operators can inspect what was collapsed.
    """

    policy = policy or StatisticalUniversePolicy()
    raw_segments = segment_completed_trades(trades)
    effective_segments, overlap = deduplicate_strategy_universe(
        raw_segments,
        policy=policy.overlap,
    )
    rankings = rank_segments(effective_segments, constraints=policy.sizing)
    candidates = select_candidates(rankings, policy=policy.selection)
    _, panel = build_daily_return_panel(
        effective_segments,
        minimum_trade_samples=policy.sizing.min_samples,
    )
    overfit = evaluate_backtest_overfit(panel, policy=policy.overfit)
    report = StatisticalUniverseReport(
        raw_segments=len(raw_segments),
        effective_segments=len(effective_segments),
        overlap=overlap,
        raw_segment_names=tuple(sorted(raw_segments)),
        effective_segment_names=tuple(sorted(effective_segments)),
        rankings=rankings,
        candidates=candidates,
        backtest_overfit=overfit,
    )
    return StatisticalUniverse(effective_segments, report)

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .eligibility import StrategyBucket
from .execution_forensics import CompletedTrade
from .heavy_tail_edge import HeavyTailEdgeReport, evaluate_selected_heavy_tail
from .hierarchical_shrinkage import (
    HierarchicalReport,
    channel_groups,
    evaluate_hierarchical_shrinkage,
    ticker_groups,
)
from .portfolio_cluster import PortfolioClusterReport, evaluate_portfolio_clusters
from .selection_adjusted import (
    SelectionAdjustedPerformance,
    evaluate_selected_segments,
)
from .sizing_lab import rank_segments, segment_completed_trades
from .strategy_selector import SelectionStatus, select_candidates
from .stress_cluster_risk import StressClusterRiskReport, evaluate_stress_cluster_risk
from .tail_dependence import TailDependenceReport, evaluate_tail_dependence


@dataclass(frozen=True, slots=True)
class QuantAuditReport:
    trades: int
    selected_segments: tuple[str, ...]
    portfolio: PortfolioClusterReport
    tail_dependence: TailDependenceReport
    stress_clusters: StressClusterRiskReport
    ticker_hierarchy: HierarchicalReport
    channel_hierarchy: HierarchicalReport
    selection_adjusted: tuple[SelectionAdjustedPerformance, ...]
    heavy_tail: tuple[HeavyTailEdgeReport, ...]
    qualified_segments: tuple[str, ...]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def run_quant_audit(trades: tuple[CompletedTrade, ...]) -> QuantAuditReport:
    segments = segment_completed_trades(trades)
    rankings = rank_segments(segments)
    candidates = select_candidates(rankings)
    selected = tuple(
        sorted(
            item.segment
            for item in candidates
            if item.status is SelectionStatus.SELECTED
        )
    )
    selected_set = set(selected)
    portfolio = evaluate_portfolio_clusters(trades)
    tail_dependence = evaluate_tail_dependence(trades)
    stress_clusters = evaluate_stress_cluster_risk(trades, tail_dependence)
    ticker_hierarchy = evaluate_hierarchical_shrinkage(ticker_groups(trades))
    channel_hierarchy = evaluate_hierarchical_shrinkage(channel_groups(trades))
    selection_adjusted = evaluate_selected_segments(segments, selected_set)
    heavy_tail = evaluate_selected_heavy_tail(segments, selected_set)
    adjusted_by_segment = {item.segment: item for item in selection_adjusted}
    heavy_by_segment = {item.segment: item for item in heavy_tail}
    qualified = tuple(
        name
        for name in selected
        if (name not in adjusted_by_segment or adjusted_by_segment[name].qualified)
        and (name not in heavy_by_segment or heavy_by_segment[name].qualified)
    )
    failures: list[str] = []
    if not portfolio.passed:
        failures.extend(f"portfolio:{item}" for item in portfolio.failures)
    if not stress_clusters.passed:
        failures.extend(f"stress_cluster:{item}" for item in stress_clusters.failures)
    failures.extend(
        f"selection_adjusted:{item.segment}:{failure}"
        for item in selection_adjusted
        if not item.qualified
        for failure in item.failures
    )
    failures.extend(
        f"heavy_tail:{item.segment}:{failure}"
        for item in heavy_tail
        if not item.qualified
        for failure in item.failures
    )
    return QuantAuditReport(
        trades=len(trades),
        selected_segments=selected,
        portfolio=portfolio,
        tail_dependence=tail_dependence,
        stress_clusters=stress_clusters,
        ticker_hierarchy=ticker_hierarchy,
        channel_hierarchy=channel_hierarchy,
        selection_adjusted=selection_adjusted,
        heavy_tail=heavy_tail,
        qualified_segments=qualified,
        failures=tuple(failures),
    )


def _int_value(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be an integer, not bool")
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError(f"{field} must be int-compatible")


def _float_value(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be numeric, not bool")
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"{field} must be float-compatible")


def _trade_from_row(row: dict[str, object]) -> CompletedTrade:
    return CompletedTrade(
        entry_event_id=str(row["entry_event_id"]),
        contract_key=str(row["contract_key"]),
        channel_id=str(row["channel_id"]),
        author_id=str(row["author_id"]),
        bucket=StrategyBucket(str(row["bucket"])),
        opened_ts_utc=datetime.fromisoformat(str(row["opened_ts_utc"])),
        closed_ts_utc=datetime.fromisoformat(str(row["closed_ts_utc"])),
        initial_quantity=_int_value(row["initial_quantity"], "initial_quantity"),
        add_count=_int_value(row["add_count"], "add_count"),
        trim_count=_int_value(row["trim_count"], "trim_count"),
        gross_premium_in=Decimal(str(row["gross_premium_in"])),
        gross_proceeds=Decimal(str(row["gross_proceeds"])),
        pnl=Decimal(str(row["pnl"])),
        return_fraction=Decimal(str(row["return_fraction"])),
        holding_seconds=_float_value(row["holding_seconds"], "holding_seconds"),
        depth_evidence_complete=bool(row.get("depth_evidence_complete", False)),
    )


def quant_audit_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run portfolio clustering, latent stress risk, hierarchical shrinkage, heavy-tail "
            "robustness, and selection-adjusted diagnostics on certified CompletedTrade JSONL. "
            "Research-only."
        )
    )
    parser.add_argument("trades", type=Path)
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in args.trades.read_text().splitlines()
        if line.strip()
    ]
    trades = tuple(_trade_from_row(row) for row in rows)
    report = run_quant_audit(trades)
    print(json.dumps(asdict(report), indent=2, sort_keys=True, default=str))

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .eligibility import StrategyBucket
from .execution_forensics import CompletedTrade
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


@dataclass(frozen=True, slots=True)
class QuantAuditReport:
    trades: int
    selected_segments: tuple[str, ...]
    portfolio: PortfolioClusterReport
    ticker_hierarchy: HierarchicalReport
    channel_hierarchy: HierarchicalReport
    selection_adjusted: tuple[SelectionAdjustedPerformance, ...]
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
    portfolio = evaluate_portfolio_clusters(trades)
    ticker_hierarchy = evaluate_hierarchical_shrinkage(ticker_groups(trades))
    channel_hierarchy = evaluate_hierarchical_shrinkage(channel_groups(trades))
    selection_adjusted = evaluate_selected_segments(
        segments,
        set(selected),
    )
    adjusted_by_segment = {item.segment: item for item in selection_adjusted}
    qualified = tuple(
        name
        for name in selected
        if name not in adjusted_by_segment or adjusted_by_segment[name].qualified
    )
    failures: list[str] = []
    if not portfolio.passed:
        failures.extend(f"portfolio:{item}" for item in portfolio.failures)
    failures.extend(
        f"selection_adjusted:{item.segment}:{failure}"
        for item in selection_adjusted
        if not item.qualified
        for failure in item.failures
    )
    return QuantAuditReport(
        trades=len(trades),
        selected_segments=selected,
        portfolio=portfolio,
        ticker_hierarchy=ticker_hierarchy,
        channel_hierarchy=channel_hierarchy,
        selection_adjusted=selection_adjusted,
        qualified_segments=qualified,
        failures=tuple(failures),
    )


def _trade_from_row(row: dict[str, object]) -> CompletedTrade:
    return CompletedTrade(
        entry_event_id=str(row["entry_event_id"]),
        contract_key=str(row["contract_key"]),
        channel_id=str(row["channel_id"]),
        author_id=str(row["author_id"]),
        bucket=StrategyBucket(str(row["bucket"])),
        opened_ts_utc=datetime.fromisoformat(str(row["opened_ts_utc"])),
        closed_ts_utc=datetime.fromisoformat(str(row["closed_ts_utc"])),
        initial_quantity=int(row["initial_quantity"]),
        add_count=int(row["add_count"]),
        trim_count=int(row["trim_count"]),
        gross_premium_in=Decimal(str(row["gross_premium_in"])),
        gross_proceeds=Decimal(str(row["gross_proceeds"])),
        pnl=Decimal(str(row["pnl"])),
        return_fraction=Decimal(str(row["return_fraction"])),
        holding_seconds=float(row["holding_seconds"]),
        depth_evidence_complete=bool(row.get("depth_evidence_complete", False)),
    )


def quant_audit_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run portfolio clustering, hierarchical shrinkage, and selection-adjusted "
            "diagnostics on certified CompletedTrade JSONL. Research-only."
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

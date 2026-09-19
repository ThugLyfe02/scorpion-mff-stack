from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .execution_forensics import (
    ExecutionProfile,
    ForensicsReport,
    ForensicStatus,
    run_archive_forensics,
)
from .history_archive import HistoryArchive
from .quote_tape import HistoricalQuoteTape


@dataclass(frozen=True, slots=True)
class LatencyScenario:
    latency_ms: int
    completed_trades: int
    mean_return: float
    median_return: float
    win_rate: float
    total_pnl: Decimal
    entry_attempts: int
    entry_fills: int
    entry_fill_rate: float
    stale_entries: int
    limit_misses: int
    missing_quote_legs: int


@dataclass(frozen=True, slots=True)
class LatencySensitivityReport:
    scenarios: tuple[LatencyScenario, ...]
    mean_return_range: float
    fill_rate_range: float
    fastest_mean_return: float
    slowest_mean_return: float

    @property
    def directionally_robust(self) -> bool:
        populated = [scenario for scenario in self.scenarios if scenario.completed_trades > 0]
        return bool(populated) and all(scenario.mean_return > 0 for scenario in populated)


def summarize_scenario(report: ForensicsReport, latency_ms: int) -> LatencyScenario:
    returns = [float(trade.return_fraction) for trade in report.completed_trades]
    wins = sum(value > 0 for value in returns)
    entry_legs = [leg for leg in report.legs if leg.event_kind.value == "ENTRY"]
    entry_fills = sum(leg.status is ForensicStatus.FILLED for leg in entry_legs)
    return LatencyScenario(
        latency_ms=latency_ms,
        completed_trades=len(returns),
        mean_return=statistics.fmean(returns) if returns else 0.0,
        median_return=statistics.median(returns) if returns else 0.0,
        win_rate=wins / len(returns) if returns else 0.0,
        total_pnl=sum((trade.pnl for trade in report.completed_trades), Decimal("0")),
        entry_attempts=len(entry_legs),
        entry_fills=entry_fills,
        entry_fill_rate=entry_fills / len(entry_legs) if entry_legs else 0.0,
        stale_entries=sum(leg.status is ForensicStatus.STALE_ENTRY for leg in entry_legs),
        limit_misses=sum(leg.status is ForensicStatus.LIMIT_NOT_FILLED for leg in entry_legs),
        missing_quote_legs=sum(leg.status is ForensicStatus.NO_QUOTE for leg in report.legs),
    )


def run_latency_sensitivity(
    archive: HistoryArchive,
    quote_tape: HistoricalQuoteTape,
    *,
    channel_ids: frozenset[str],
    allowed_author_ids: frozenset[str],
    latency_grid_ms: tuple[int, ...] = (50, 100, 250, 500, 1000, 2000),
    include_research_only: bool = False,
) -> LatencySensitivityReport:
    if not latency_grid_ms or any(value < 0 for value in latency_grid_ms):
        raise ValueError("latency_grid_ms must contain non-negative values")
    scenarios: list[LatencyScenario] = []
    for latency_ms in sorted(set(latency_grid_ms)):
        report = run_archive_forensics(
            archive,
            quote_tape,
            channel_ids=channel_ids,
            allowed_author_ids=allowed_author_ids,
            profile=ExecutionProfile(decision_latency=timedelta(milliseconds=latency_ms)),
            include_research_only=include_research_only,
        )
        scenarios.append(summarize_scenario(report, latency_ms))
    means = [scenario.mean_return for scenario in scenarios]
    fill_rates = [scenario.entry_fill_rate for scenario in scenarios]
    return LatencySensitivityReport(
        scenarios=tuple(scenarios),
        mean_return_range=max(means) - min(means) if means else 0.0,
        fill_rate_range=max(fill_rates) - min(fill_rates) if fill_rates else 0.0,
        fastest_mean_return=scenarios[0].mean_return,
        slowest_mean_return=scenarios[-1].mean_return,
    )

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import timedelta

from .execution_forensics import ExecutionProfile, run_archive_forensics
from .history_archive import HistoryArchive
from .policy_bundle import RuntimePolicyBundle
from .quote_tape import HistoricalQuoteTape
from .sizing_lab import SizingConstraints, score_segment


@dataclass(frozen=True, slots=True)
class ExecutionScenarioSpec:
    latency_ms: int
    quote_max_lag_ms: int
    limit_wait_ms: int
    depth_supported_only: bool = True

    def __post_init__(self) -> None:
        if min(self.latency_ms, self.quote_max_lag_ms, self.limit_wait_ms) < 0:
            raise ValueError("execution scenario timings cannot be negative")


@dataclass(frozen=True, slots=True)
class ExecutionScenarioResult:
    spec: ExecutionScenarioSpec
    completed_trades: int
    analyzed_trades: int
    mean_return: float
    median_return: float
    conservative_edge: float
    win_rate: float
    positive_fold_ratio: float
    max_drawdown: float
    passed: bool
    failure: str


@dataclass(frozen=True, slots=True)
class ExecutionUncertaintyEnvelope:
    scenarios: tuple[ExecutionScenarioResult, ...]
    populated_scenarios: int
    passing_scenarios: int
    worst_conservative_edge: float
    worst_mean_return: float
    worst_win_rate: float
    robust: bool
    failures: tuple[str, ...]


def compact_execution_scenarios() -> tuple[ExecutionScenarioSpec, ...]:
    """High-information diagonal stress grid for routine forensic runs."""
    return (
        ExecutionScenarioSpec(50, 500, 1000),
        ExecutionScenarioSpec(100, 500, 3000),
        ExecutionScenarioSpec(250, 1000, 3000),
        ExecutionScenarioSpec(500, 1000, 5000),
        ExecutionScenarioSpec(1000, 3000, 5000),
        ExecutionScenarioSpec(2000, 3000, 5000),
    )


def default_execution_scenarios() -> tuple[ExecutionScenarioSpec, ...]:
    """Exhaustive research grid; intentionally more expensive than the compact set."""
    return tuple(
        ExecutionScenarioSpec(latency, quote_lag, limit_wait, depth_supported_only=True)
        for latency in (50, 250, 500, 1000, 2000)
        for quote_lag in (500, 1000, 3000)
        for limit_wait in (1000, 3000, 5000)
    )


def run_execution_uncertainty_envelope(
    archive: HistoryArchive,
    quote_tape: HistoricalQuoteTape,
    *,
    channel_ids: frozenset[str],
    allowed_author_ids: frozenset[str],
    runtime_policy: RuntimePolicyBundle | None = None,
    scenarios: tuple[ExecutionScenarioSpec, ...] | None = None,
    constraints: SizingConstraints | None = None,
    minimum_scenario_samples: int = 20,
    minimum_passing_fraction: float = 0.80,
) -> ExecutionUncertaintyEnvelope:
    if minimum_scenario_samples <= 0:
        raise ValueError("minimum_scenario_samples must be positive")
    if not 0.0 < minimum_passing_fraction <= 1.0:
        raise ValueError("minimum_passing_fraction must be in (0,1]")
    scenarios = scenarios or default_execution_scenarios()
    if not scenarios:
        raise ValueError("at least one execution scenario is required")
    constraints = constraints or SizingConstraints(min_samples=minimum_scenario_samples)
    runtime_policy = runtime_policy or RuntimePolicyBundle()
    results: list[ExecutionScenarioResult] = []

    for spec in scenarios:
        report = run_archive_forensics(
            archive,
            quote_tape,
            channel_ids=channel_ids,
            allowed_author_ids=allowed_author_ids,
            profile=ExecutionProfile(
                decision_latency=timedelta(milliseconds=spec.latency_ms),
                quote_max_lag=timedelta(milliseconds=spec.quote_max_lag_ms),
                entry_limit_wait=timedelta(milliseconds=spec.limit_wait_ms),
            ),
            runtime_policy=runtime_policy,
        )
        trades = tuple(
            trade
            for trade in report.completed_trades
            if not spec.depth_supported_only or trade.depth_evidence_complete
        )
        values = [float(item.return_fraction) for item in trades]
        if len(trades) < minimum_scenario_samples:
            results.append(
                ExecutionScenarioResult(
                    spec=spec,
                    completed_trades=len(report.completed_trades),
                    analyzed_trades=len(trades),
                    mean_return=statistics.fmean(values) if values else 0.0,
                    median_return=statistics.median(values) if values else 0.0,
                    conservative_edge=0.0,
                    win_rate=(
                        sum(value > 0 for value in values) / len(values) if values else 0.0
                    ),
                    positive_fold_ratio=0.0,
                    max_drawdown=0.0,
                    passed=False,
                    failure="insufficient_scenario_samples",
                )
            )
            continue
        metric = score_segment("uncertainty", trades, constraints=constraints)
        passed = metric.conservative_edge > 0 and metric.positive_fold_ratio >= 0.75
        results.append(
            ExecutionScenarioResult(
                spec=spec,
                completed_trades=len(report.completed_trades),
                analyzed_trades=len(trades),
                mean_return=metric.mean_return,
                median_return=metric.median_return,
                conservative_edge=metric.conservative_edge,
                win_rate=metric.win_rate,
                positive_fold_ratio=metric.positive_fold_ratio,
                max_drawdown=metric.max_drawdown,
                passed=passed,
                failure="" if passed else "non_robust_conservative_edge_or_time_folds",
            )
        )

    populated = [item for item in results if item.analyzed_trades >= minimum_scenario_samples]
    passing = [item for item in results if item.passed]
    insufficient = [item for item in results if item.analyzed_trades < minimum_scenario_samples]
    required = max(1, int(len(results) * minimum_passing_fraction + 0.999999))
    failures: list[str] = []
    if not populated:
        failures.append("no_execution_scenario_has_sufficient_samples")
    if insufficient:
        failures.append(f"insufficient_execution_scenarios:{len(insufficient)}")
    if len(passing) < required:
        failures.append(f"passing_scenarios:{len(passing)}<{required}")
    if results and min(item.conservative_edge for item in results) <= 0:
        failures.append("worst_case_conservative_edge_non_positive")

    return ExecutionUncertaintyEnvelope(
        scenarios=tuple(results),
        populated_scenarios=len(populated),
        passing_scenarios=len(passing),
        worst_conservative_edge=min((item.conservative_edge for item in results), default=0.0),
        worst_mean_return=min((item.mean_return for item in results), default=0.0),
        worst_win_rate=min((item.win_rate for item in results), default=0.0),
        robust=not failures,
        failures=tuple(failures),
    )

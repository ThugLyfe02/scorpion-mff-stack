from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .accuracy import percentile


@dataclass(frozen=True, slots=True)
class LatencyBudget:
    parser_p95_us: int = 2_000
    pipeline_p95_us: int = 20_000
    receive_to_review_p95: timedelta = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class LatencyReport:
    count: int
    parser_p50_us: float
    parser_p95_us: float
    parser_p99_us: float
    pipeline_p50_us: float
    pipeline_p95_us: float
    pipeline_p99_us: float
    within_budget: bool


def evaluate_latency(
    parser_samples_us: list[int],
    pipeline_samples_us: list[int],
    budget: LatencyBudget | None = None,
) -> LatencyReport:
    budget = budget or LatencyBudget()
    if len(parser_samples_us) != len(pipeline_samples_us):
        raise ValueError("parser and pipeline sample counts must match")
    parser_p95 = percentile(parser_samples_us, 0.95)
    pipeline_p95 = percentile(pipeline_samples_us, 0.95)
    return LatencyReport(
        count=len(parser_samples_us),
        parser_p50_us=percentile(parser_samples_us, 0.50),
        parser_p95_us=parser_p95,
        parser_p99_us=percentile(parser_samples_us, 0.99),
        pipeline_p50_us=percentile(pipeline_samples_us, 0.50),
        pipeline_p95_us=pipeline_p95,
        pipeline_p99_us=percentile(pipeline_samples_us, 0.99),
        within_budget=(
            parser_p95 <= budget.parser_p95_us
            and pipeline_p95 <= budget.pipeline_p95_us
        ),
    )

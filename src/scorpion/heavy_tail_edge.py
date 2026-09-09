from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from enum import StrEnum

from .execution_forensics import CompletedTrade


class HeavyTailStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class HeavyTailPolicy:
    minimum_samples: int = 30
    blocks: int = 5
    minimum_block_samples: int = 5
    bootstrap_trials: int = 2000
    lower_percentile: float = 0.10
    minimum_positive_block_ratio: float = 0.60
    minimum_robust_lower_bound: float = 0.0
    random_seed: int = 160021

    def __post_init__(self) -> None:
        if self.minimum_samples <= 0 or self.minimum_block_samples <= 0:
            raise ValueError("sample thresholds must be positive")
        if self.blocks < 3:
            raise ValueError("blocks must be >=3")
        if self.bootstrap_trials < 100:
            raise ValueError("bootstrap_trials must be >=100")
        if not 0 < self.lower_percentile < 0.5:
            raise ValueError("lower_percentile must be in (0,0.5)")
        if not 0 <= self.minimum_positive_block_ratio <= 1:
            raise ValueError("minimum_positive_block_ratio must be in [0,1]")


@dataclass(frozen=True, slots=True)
class HeavyTailEdgeReport:
    segment: str
    samples: int
    blocks: int
    ordinary_mean: float
    median_of_means: float
    bootstrap_lower_bound: float
    positive_block_ratio: float
    ordinary_minus_robust: float
    block_means: tuple[float, ...]
    status: HeavyTailStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is HeavyTailStatus.QUALIFIED


def _chronological_blocks(
    trades: tuple[CompletedTrade, ...],
    blocks: int,
) -> tuple[tuple[CompletedTrade, ...], ...]:
    ordered = tuple(sorted(trades, key=lambda item: (item.opened_ts_utc, item.entry_event_id)))
    effective = min(blocks, len(ordered))
    output: list[tuple[CompletedTrade, ...]] = []
    for index in range(effective):
        start = index * len(ordered) // effective
        end = (index + 1) * len(ordered) // effective
        part = ordered[start:end]
        if part:
            output.append(part)
    return tuple(output)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(fraction * (len(ordered) - 1))))
    return ordered[index]


def evaluate_heavy_tail_edge(
    segment: str,
    trades: tuple[CompletedTrade, ...],
    *,
    policy: HeavyTailPolicy | None = None,
) -> HeavyTailEdgeReport:
    """Estimate edge robustly when a few extreme option returns may dominate the sample.

    This is a research robustness diagnostic, not a formal distribution-free confidence bound.
    The bootstrap is performed over chronological block means so individual hero trades cannot
    directly dominate every resample.
    """
    policy = policy or HeavyTailPolicy()
    if len(trades) < policy.minimum_samples:
        return HeavyTailEdgeReport(
            segment=segment,
            samples=len(trades),
            blocks=0,
            ordinary_mean=0.0,
            median_of_means=0.0,
            bootstrap_lower_bound=0.0,
            positive_block_ratio=0.0,
            ordinary_minus_robust=0.0,
            block_means=(),
            status=HeavyTailStatus.INSUFFICIENT,
            failures=(f"insufficient_samples:{len(trades)}<{policy.minimum_samples}",),
        )

    blocks = _chronological_blocks(trades, policy.blocks)
    if len(blocks) < 3 or min(len(block) for block in blocks) < policy.minimum_block_samples:
        return HeavyTailEdgeReport(
            segment=segment,
            samples=len(trades),
            blocks=len(blocks),
            ordinary_mean=0.0,
            median_of_means=0.0,
            bootstrap_lower_bound=0.0,
            positive_block_ratio=0.0,
            ordinary_minus_robust=0.0,
            block_means=(),
            status=HeavyTailStatus.INSUFFICIENT,
            failures=("insufficient_chronological_block_depth",),
        )

    values = [float(item.return_fraction) for item in trades]
    block_means = tuple(
        statistics.fmean(float(item.return_fraction) for item in block)
        for block in blocks
    )
    robust = statistics.median(block_means)
    rng = random.Random(policy.random_seed)
    bootstrapped: list[float] = []
    for _ in range(policy.bootstrap_trials):
        sampled = [block_means[rng.randrange(len(block_means))] for _ in block_means]
        bootstrapped.append(statistics.median(sampled))
    lower = _percentile(bootstrapped, policy.lower_percentile)
    positive_ratio = sum(value > 0 for value in block_means) / len(block_means)
    ordinary = statistics.fmean(values)
    failures: list[str] = []
    if lower <= policy.minimum_robust_lower_bound:
        failures.append(
            "robust_bootstrap_lower_bound_not_positive:"
            f"{lower:.6f}<={policy.minimum_robust_lower_bound:.6f}"
        )
    if positive_ratio < policy.minimum_positive_block_ratio:
        failures.append(
            "positive_chronological_block_ratio_below_threshold:"
            f"{positive_ratio:.6f}<{policy.minimum_positive_block_ratio:.6f}"
        )
    status = HeavyTailStatus.QUALIFIED if not failures else HeavyTailStatus.FAILED
    return HeavyTailEdgeReport(
        segment=segment,
        samples=len(trades),
        blocks=len(block_means),
        ordinary_mean=ordinary,
        median_of_means=robust,
        bootstrap_lower_bound=lower,
        positive_block_ratio=positive_ratio,
        ordinary_minus_robust=ordinary - robust,
        block_means=block_means,
        status=status,
        failures=tuple(failures),
    )


def evaluate_selected_heavy_tail(
    segments: dict[str, tuple[CompletedTrade, ...]],
    selected: set[str],
    *,
    policy: HeavyTailPolicy | None = None,
) -> tuple[HeavyTailEdgeReport, ...]:
    return tuple(
        evaluate_heavy_tail_edge(name, segments[name], policy=policy)
        for name in sorted(selected)
        if name in segments
    )

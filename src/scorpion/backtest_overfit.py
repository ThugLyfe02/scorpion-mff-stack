from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo

from .execution_forensics import CompletedTrade

_MARKET_TZ = ZoneInfo("America/New_York")


class OverfitStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class OverfitPolicy:
    slices: int = 8
    minimum_periods: int = 24
    minimum_strategies: int = 2
    maximum_probability_of_backtest_overfit: float = 0.25
    maximum_combinations: int = 256

    def __post_init__(self) -> None:
        if self.slices < 4 or self.slices % 2:
            raise ValueError("slices must be an even integer >=4")
        if self.minimum_periods < self.slices:
            raise ValueError("minimum_periods must be >= slices")
        if self.minimum_strategies < 2:
            raise ValueError("minimum_strategies must be >=2")
        if not 0 <= self.maximum_probability_of_backtest_overfit <= 1:
            raise ValueError("maximum PBO must be in [0,1]")
        if self.maximum_combinations <= 0:
            raise ValueError("maximum_combinations must be positive")


@dataclass(frozen=True, slots=True)
class OverfitSplit:
    split_id: int
    selected_strategy: str
    in_sample_mean: float
    out_of_sample_mean: float
    out_of_sample_rank_percentile: float
    logit_rank: float


@dataclass(frozen=True, slots=True)
class BacktestOverfitReport:
    strategies: int
    periods: int
    evaluated_splits: int
    probability_of_backtest_overfit: float
    median_selected_oos_rank_percentile: float
    mean_selected_oos_return: float
    selection_entropy: float
    status: OverfitStatus
    failures: tuple[str, ...]
    splits: tuple[OverfitSplit, ...]

    @property
    def passed(self) -> bool:
        return self.status in {OverfitStatus.PASS, OverfitStatus.NOT_APPLICABLE}


def _daily_key(trade: CompletedTrade) -> str:
    return trade.opened_ts_utc.astimezone(_MARKET_TZ).date().isoformat()


def build_daily_return_panel(
    segments: dict[str, tuple[CompletedTrade, ...]],
    *,
    minimum_trade_samples: int = 1,
) -> tuple[tuple[str, ...], dict[str, tuple[float, ...]]]:
    if minimum_trade_samples <= 0:
        raise ValueError("minimum_trade_samples must be positive")
    eligible = {
        name: trades
        for name, trades in segments.items()
        if len(trades) >= minimum_trade_samples
    }
    dates = tuple(
        sorted({_daily_key(trade) for trades in eligible.values() for trade in trades})
    )
    panel: dict[str, tuple[float, ...]] = {}
    for name, trades in eligible.items():
        by_day: dict[str, float] = {}
        for trade in trades:
            key = _daily_key(trade)
            by_day[key] = by_day.get(key, 0.0) + float(trade.return_fraction)
        panel[name] = tuple(by_day.get(day, 0.0) for day in dates)
    return dates, panel


def _slice_indices(periods: int, slices: int) -> tuple[tuple[int, ...], ...]:
    result: list[tuple[int, ...]] = []
    for index in range(slices):
        start = index * periods // slices
        end = (index + 1) * periods // slices
        result.append(tuple(range(start, end)))
    return tuple(result)


def _mean_at(values: tuple[float, ...], indices: tuple[int, ...]) -> float:
    if not indices:
        return 0.0
    return statistics.fmean(values[index] for index in indices)


def _average_rank_percentile(selected: float, values: tuple[float, ...]) -> float:
    if len(values) <= 1:
        return 1.0
    lower = sum(value < selected for value in values)
    equal = sum(value == selected for value in values)
    average_rank = lower + (equal + 1) / 2.0
    return average_rank / len(values)


def _entropy(counts: dict[str, int], total: int) -> float:
    if total <= 0 or len(counts) <= 1:
        return 0.0
    entropy = 0.0
    for count in counts.values():
        if count <= 0:
            continue
        probability = count / total
        entropy -= probability * math.log(probability)
    return entropy / math.log(len(counts))


def evaluate_backtest_overfit(
    panel: dict[str, tuple[float, ...]],
    *,
    policy: OverfitPolicy | None = None,
) -> BacktestOverfitReport:
    policy = policy or OverfitPolicy()
    if len(panel) < policy.minimum_strategies:
        return BacktestOverfitReport(
            strategies=len(panel),
            periods=max((len(values) for values in panel.values()), default=0),
            evaluated_splits=0,
            probability_of_backtest_overfit=0.0,
            median_selected_oos_rank_percentile=0.0,
            mean_selected_oos_return=0.0,
            selection_entropy=0.0,
            status=OverfitStatus.NOT_APPLICABLE,
            failures=(),
            splits=(),
        )
    lengths = {len(values) for values in panel.values()}
    if len(lengths) != 1:
        raise ValueError("all strategy return series must have equal length")
    periods = lengths.pop()
    if periods < policy.minimum_periods:
        return BacktestOverfitReport(
            strategies=len(panel),
            periods=periods,
            evaluated_splits=0,
            probability_of_backtest_overfit=0.0,
            median_selected_oos_rank_percentile=0.0,
            mean_selected_oos_return=0.0,
            selection_entropy=0.0,
            status=OverfitStatus.INSUFFICIENT,
            failures=(f"insufficient_periods:{periods}<{policy.minimum_periods}",),
            splits=(),
        )

    names = tuple(sorted(panel))
    slices = _slice_indices(periods, policy.slices)
    train_slice_count = policy.slices // 2
    combinations = list(itertools.combinations(range(policy.slices), train_slice_count))
    if len(combinations) > policy.maximum_combinations:
        stride = len(combinations) / policy.maximum_combinations
        combinations = [
            combinations[min(len(combinations) - 1, int(index * stride))]
            for index in range(policy.maximum_combinations)
        ]

    splits: list[OverfitSplit] = []
    selected_counts: dict[str, int] = {}
    for split_id, train_slices in enumerate(combinations):
        train_set = set(train_slices)
        train_indices = tuple(
            index
            for slice_index, indices in enumerate(slices)
            if slice_index in train_set
            for index in indices
        )
        test_indices = tuple(
            index
            for slice_index, indices in enumerate(slices)
            if slice_index not in train_set
            for index in indices
        )
        in_means = {name: _mean_at(panel[name], train_indices) for name in names}
        selected = max(names, key=lambda name: (in_means[name], name))
        out_means = tuple(_mean_at(panel[name], test_indices) for name in names)
        selected_oos = _mean_at(panel[selected], test_indices)
        percentile = _average_rank_percentile(selected_oos, out_means)
        clipped = min(1.0 - 1e-12, max(1e-12, percentile))
        logit = math.log(clipped / (1.0 - clipped))
        selected_counts[selected] = selected_counts.get(selected, 0) + 1
        splits.append(
            OverfitSplit(
                split_id=split_id,
                selected_strategy=selected,
                in_sample_mean=in_means[selected],
                out_of_sample_mean=selected_oos,
                out_of_sample_rank_percentile=percentile,
                logit_rank=logit,
            )
        )

    pbo = sum(split.logit_rank <= 0 for split in splits) / len(splits)
    median_rank = statistics.median(split.out_of_sample_rank_percentile for split in splits)
    mean_oos = statistics.fmean(split.out_of_sample_mean for split in splits)
    failures: list[str] = []
    if pbo > policy.maximum_probability_of_backtest_overfit:
        failures.append(
            "probability_of_backtest_overfit_above_threshold:"
            f"{pbo:.6f}>{policy.maximum_probability_of_backtest_overfit:.6f}"
        )
    status = OverfitStatus.FAIL if failures else OverfitStatus.PASS
    return BacktestOverfitReport(
        strategies=len(names),
        periods=periods,
        evaluated_splits=len(splits),
        probability_of_backtest_overfit=pbo,
        median_selected_oos_rank_percentile=median_rank,
        mean_selected_oos_return=mean_oos,
        selection_entropy=_entropy(selected_counts, len(splits)),
        status=status,
        failures=tuple(failures),
        splits=tuple(splits),
    )

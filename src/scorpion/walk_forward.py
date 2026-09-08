from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from datetime import timedelta

from .execution_forensics import CompletedTrade
from .sizing_lab import SizingConstraints, score_segment


@dataclass(frozen=True, slots=True)
class WalkForwardPolicy:
    min_train_samples: int = 30
    test_size: int = 10
    min_folds: int = 3
    embargo: timedelta = timedelta(minutes=5)
    minimum_positive_fold_ratio: float = 2.0 / 3.0
    minimum_pooled_oos_mean: float = 0.0
    maximum_worst_fold_loss: float = 0.25

    def __post_init__(self) -> None:
        if self.min_train_samples <= 0 or self.test_size <= 0 or self.min_folds <= 0:
            raise ValueError("walk-forward sample/fold counts must be positive")
        if self.embargo < timedelta(0):
            raise ValueError("embargo cannot be negative")
        if not 0.0 <= self.minimum_positive_fold_ratio <= 1.0:
            raise ValueError("minimum_positive_fold_ratio must be in [0,1]")
        if self.maximum_worst_fold_loss < 0.0:
            raise ValueError("maximum_worst_fold_loss cannot be negative")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold: int
    train_samples: int
    test_samples: int
    train_end_ts: str
    test_start_ts: str
    test_end_ts: str
    train_conservative_edge: float
    test_mean_return: float
    test_win_rate: float
    test_max_drawdown: float


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    folds: tuple[WalkForwardFold, ...]
    oos_samples: int
    pooled_oos_mean: float
    positive_fold_ratio: float
    worst_fold_mean: float
    passed: bool
    failures: tuple[str, ...]


def _ordered(trades: tuple[CompletedTrade, ...]) -> tuple[CompletedTrade, ...]:
    return tuple(
        sorted(
            trades,
            key=lambda trade: (
                trade.opened_ts_utc,
                trade.closed_ts_utc,
                trade.entry_event_id,
            ),
        )
    )


def evaluate_walk_forward(
    trades: tuple[CompletedTrade, ...],
    *,
    policy: WalkForwardPolicy | None = None,
    sizing_constraints: SizingConstraints | None = None,
) -> WalkForwardReport:
    policy = policy or WalkForwardPolicy()
    constraints = sizing_constraints or SizingConstraints()
    ordered = _ordered(trades)
    folds: list[WalkForwardFold] = []
    oos_returns: list[float] = []
    fold_means: list[float] = []

    start = policy.min_train_samples
    fold_number = 0
    while start < len(ordered):
        test = ordered[start : start + policy.test_size]
        if not test:
            break
        test_start = test[0].opened_ts_utc
        cutoff = test_start - policy.embargo
        train = tuple(trade for trade in ordered[:start] if trade.closed_ts_utc <= cutoff)
        if len(train) < policy.min_train_samples:
            start += policy.test_size
            continue

        fold_number += 1
        train_metrics = score_segment(
            f"walk-forward-train-{fold_number}",
            train,
            constraints=constraints,
        )
        oos_constraints = replace(
            constraints,
            min_samples=1,
            max_fdr_q_value=1.0,
            bootstrap_resamples=max(100, min(constraints.bootstrap_resamples, 500)),
        )
        test_metrics = score_segment(
            f"walk-forward-test-{fold_number}",
            tuple(test),
            constraints=oos_constraints,
        )
        fold_means.append(test_metrics.mean_return)
        oos_returns.extend(float(trade.return_fraction) for trade in test)
        folds.append(
            WalkForwardFold(
                fold=fold_number,
                train_samples=len(train),
                test_samples=len(test),
                train_end_ts=max(trade.closed_ts_utc for trade in train).isoformat(),
                test_start_ts=test[0].opened_ts_utc.isoformat(),
                test_end_ts=max(trade.closed_ts_utc for trade in test).isoformat(),
                train_conservative_edge=train_metrics.conservative_edge,
                test_mean_return=test_metrics.mean_return,
                test_win_rate=test_metrics.win_rate,
                test_max_drawdown=test_metrics.max_drawdown,
            )
        )
        start += policy.test_size

    pooled = statistics.fmean(oos_returns) if oos_returns else 0.0
    positive_ratio = (
        sum(value > 0.0 for value in fold_means) / len(fold_means) if fold_means else 0.0
    )
    worst = min(fold_means) if fold_means else 0.0
    failures: list[str] = []
    if len(folds) < policy.min_folds:
        failures.append(f"folds:{len(folds)}<{policy.min_folds}")
    if pooled <= policy.minimum_pooled_oos_mean:
        failures.append(
            f"pooled_oos_mean:{pooled:.6f}<={policy.minimum_pooled_oos_mean:.6f}"
        )
    if positive_ratio < policy.minimum_positive_fold_ratio:
        failures.append(
            "positive_fold_ratio:"
            f"{positive_ratio:.6f}<{policy.minimum_positive_fold_ratio:.6f}"
        )
    if fold_means and worst < -policy.maximum_worst_fold_loss:
        failures.append(
            f"worst_fold_mean:{worst:.6f}<-{policy.maximum_worst_fold_loss:.6f}"
        )

    return WalkForwardReport(
        folds=tuple(folds),
        oos_samples=len(oos_returns),
        pooled_oos_mean=pooled,
        positive_fold_ratio=positive_ratio,
        worst_fold_mean=worst,
        passed=not failures,
        failures=tuple(failures),
    )

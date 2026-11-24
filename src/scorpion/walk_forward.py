from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

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
    minimum_unique_oos_days: int = 3
    minimum_pooled_oos_lower_bound: float = 0.0
    bootstrap_resamples: int = 1000
    bootstrap_block_size: int = 3
    bootstrap_alpha: float = 0.05
    bootstrap_seed: int = 17
    max_training_lookback: timedelta | None = None

    def __post_init__(self) -> None:
        if self.min_train_samples <= 0 or self.test_size <= 0 or self.min_folds <= 0:
            raise ValueError("walk-forward sample/fold counts must be positive")
        if self.embargo < timedelta(0):
            raise ValueError("embargo cannot be negative")
        if not 0.0 <= self.minimum_positive_fold_ratio <= 1.0:
            raise ValueError("minimum_positive_fold_ratio must be in [0,1]")
        if self.maximum_worst_fold_loss < 0.0:
            raise ValueError("maximum_worst_fold_loss cannot be negative")
        if self.minimum_unique_oos_days <= 0:
            raise ValueError("minimum_unique_oos_days must be positive")
        if self.bootstrap_resamples <= 0 or self.bootstrap_block_size <= 0:
            raise ValueError("bootstrap settings must be positive")
        if not 0.0 < self.bootstrap_alpha < 0.5:
            raise ValueError("bootstrap_alpha must be in (0,0.5)")
        if self.max_training_lookback is not None and self.max_training_lookback <= timedelta(0):
            raise ValueError("max_training_lookback must be positive when provided")


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
    purged_train_samples: int = 0
    stale_train_samples_dropped: int = 0
    embargo_gap_seconds: float = 0.0
    temporal_overlap_detected: bool = False


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    folds: tuple[WalkForwardFold, ...]
    oos_samples: int
    pooled_oos_mean: float
    positive_fold_ratio: float
    worst_fold_mean: float
    passed: bool
    failures: tuple[str, ...]
    pooled_oos_lower_bound: float = 0.0
    unique_oos_days: int = 0
    total_purged_train_samples: int = 0
    total_stale_train_samples_dropped: int = 0
    temporal_leakage_detected: bool = False


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


def _daily_oos_means(trades: list[CompletedTrade]) -> list[float]:
    grouped: dict[date, list[float]] = {}
    for trade in trades:
        day = trade.opened_ts_utc.date()
        grouped.setdefault(day, []).append(float(trade.return_fraction))
    return [statistics.fmean(grouped[day]) for day in sorted(grouped)]


def _circular_block_bootstrap_lower_bound(
    values: list[float],
    *,
    resamples: int,
    block_size: int,
    alpha: float,
    seed: int,
) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rng = random.Random(seed)
    block = min(block_size, len(values))
    estimates: list[float] = []
    for _ in range(resamples):
        sample: list[float] = []
        while len(sample) < len(values):
            start = rng.randrange(len(values))
            sample.extend(values[(start + offset) % len(values)] for offset in range(block))
        estimates.append(statistics.fmean(sample[: len(values)]))
    estimates.sort()
    index = min(len(estimates) - 1, max(0, int(alpha * len(estimates))))
    return estimates[index]


def evaluate_walk_forward(
    trades: tuple[CompletedTrade, ...],
    *,
    policy: WalkForwardPolicy | None = None,
    sizing_constraints: SizingConstraints | None = None,
) -> WalkForwardReport:
    """Purged chronological walk-forward evaluation with an OOS confidence gate.

    Every fold removes training lifecycles whose labels extend into the embargo before the
    test window. Optional training lookback bounds stale-regime influence. Pooled OOS evidence
    is aggregated by day and receives a deterministic circular block-bootstrap lower bound so
    correlated intraday trades cannot manufacture confidence through raw trade count alone.
    """
    policy = policy or WalkForwardPolicy()
    constraints = sizing_constraints or SizingConstraints()
    ordered = _ordered(trades)
    folds: list[WalkForwardFold] = []
    oos_trades: list[CompletedTrade] = []
    fold_means: list[float] = []
    total_purged = 0
    total_stale = 0
    leakage_detected = False

    start = policy.min_train_samples
    fold_number = 0
    while start < len(ordered):
        test = ordered[start : start + policy.test_size]
        if not test:
            break
        test_start = min(trade.opened_ts_utc for trade in test)
        cutoff = test_start - policy.embargo
        candidate_train = ordered[:start]
        purged = tuple(trade for trade in candidate_train if trade.closed_ts_utc > cutoff)
        train = tuple(trade for trade in candidate_train if trade.closed_ts_utc <= cutoff)
        stale_dropped: tuple[CompletedTrade, ...] = ()
        if policy.max_training_lookback is not None:
            oldest_allowed = test_start - policy.max_training_lookback
            stale_dropped = tuple(
                trade for trade in train if trade.opened_ts_utc < oldest_allowed
            )
            train = tuple(
                trade for trade in train if trade.opened_ts_utc >= oldest_allowed
            )

        if len(train) < policy.min_train_samples:
            start += policy.test_size
            continue

        fold_number += 1
        total_purged += len(purged)
        total_stale += len(stale_dropped)
        overlap = any(trade.closed_ts_utc > cutoff for trade in train)
        leakage_detected = leakage_detected or overlap
        latest_train_close = max(trade.closed_ts_utc for trade in train)
        embargo_gap = max(0.0, (test_start - latest_train_close).total_seconds())

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
        oos_trades.extend(test)
        folds.append(
            WalkForwardFold(
                fold=fold_number,
                train_samples=len(train),
                test_samples=len(test),
                train_end_ts=latest_train_close.isoformat(),
                test_start_ts=test_start.isoformat(),
                test_end_ts=max(trade.closed_ts_utc for trade in test).isoformat(),
                train_conservative_edge=train_metrics.conservative_edge,
                test_mean_return=test_metrics.mean_return,
                test_win_rate=test_metrics.win_rate,
                test_max_drawdown=test_metrics.max_drawdown,
                purged_train_samples=len(purged),
                stale_train_samples_dropped=len(stale_dropped),
                embargo_gap_seconds=embargo_gap,
                temporal_overlap_detected=overlap,
            )
        )
        start += policy.test_size

    oos_returns = [float(trade.return_fraction) for trade in oos_trades]
    pooled = statistics.fmean(oos_returns) if oos_returns else 0.0
    positive_ratio = (
        sum(value > 0.0 for value in fold_means) / len(fold_means) if fold_means else 0.0
    )
    worst = min(fold_means) if fold_means else 0.0
    daily_means = _daily_oos_means(oos_trades)
    lower_bound = _circular_block_bootstrap_lower_bound(
        daily_means,
        resamples=policy.bootstrap_resamples,
        block_size=policy.bootstrap_block_size,
        alpha=policy.bootstrap_alpha,
        seed=policy.bootstrap_seed,
    )

    failures: list[str] = []
    if len(folds) < policy.min_folds:
        failures.append(f"folds:{len(folds)}<{policy.min_folds}")
    if pooled <= policy.minimum_pooled_oos_mean:
        failures.append(
            f"pooled_oos_mean:{pooled:.6f}<={policy.minimum_pooled_oos_mean:.6f}"
        )
    if len(daily_means) < policy.minimum_unique_oos_days:
        failures.append(
            f"unique_oos_days:{len(daily_means)}<{policy.minimum_unique_oos_days}"
        )
    if daily_means and lower_bound <= policy.minimum_pooled_oos_lower_bound:
        failures.append(
            "pooled_oos_lower_bound:"
            f"{lower_bound:.6f}<={policy.minimum_pooled_oos_lower_bound:.6f}"
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
    if leakage_detected:
        failures.append("temporal_label_overlap_detected_after_purge")

    return WalkForwardReport(
        folds=tuple(folds),
        oos_samples=len(oos_returns),
        pooled_oos_mean=pooled,
        positive_fold_ratio=positive_ratio,
        worst_fold_mean=worst,
        passed=not failures,
        failures=tuple(failures),
        pooled_oos_lower_bound=lower_bound,
        unique_oos_days=len(daily_means),
        total_purged_train_samples=total_purged,
        total_stale_train_samples_dropped=total_stale,
        temporal_leakage_detected=leakage_detected,
    )


@dataclass(frozen=True, slots=True)
class FrozenValidationWindow:
    """Declared training/validation boundary; not a substitute for actual sample-level purge."""

    train_through: datetime
    training_labels_available_through: datetime
    validation_start: datetime
    validation_end: datetime
    validation_labels_available_through: datetime
    training_data_hashes: tuple[str, ...]
    validation_data_hash: str


@dataclass(frozen=True, slots=True)
class FrozenValidationLineageReport:
    folds: int
    valid: bool
    failures: tuple[str, ...]


def verify_frozen_validation_windows(
    windows: tuple[FrozenValidationWindow, ...], *, embargo: timedelta, frozen: datetime,
    declared_data: frozenset[str], minimum_folds: int = 3,
) -> FrozenValidationLineageReport:
    """Reject declared temporal and dataset overlap in a frozen model's development lineage.

    Later training may reuse an earlier validation set only after its outcomes were
    available. This checks provenance boundaries, not correctness of the declared dataset
    content or measured held-out performance.
    """
    if embargo < timedelta(0) or minimum_folds < 0 or len(windows) > 1000:
        raise ValueError("invalid frozen lineage policy")
    if frozen.tzinfo is None or frozen.utcoffset() is None:
        raise ValueError("naive artifact freeze timestamp")
    failures: list[str] = []
    if len(windows) < minimum_folds:
        failures.append("insufficient_declared_validation_folds")
    prior_end: datetime | None = None
    prior_train: datetime | None = None
    prior_training_labels: datetime | None = None
    prior_validation_labels: dict[str, datetime] = {}
    all_validation_ids = {window.validation_data_hash for window in windows}
    for index, fold in enumerate(windows):
        stamps = (
            fold.train_through, fold.training_labels_available_through, fold.validation_start,
            fold.validation_end, fold.validation_labels_available_through,
        )
        if any(stamp.tzinfo is None or stamp.utcoffset() is None for stamp in stamps):
            failures.append(f"naive_validation_window:{index}")
            continue
        if not (
            fold.train_through <= fold.training_labels_available_through
            <= fold.validation_start - embargo
            and fold.training_labels_available_through < fold.validation_start
            < fold.validation_end <= fold.validation_labels_available_through <= frozen
        ):
            failures.append(f"fold_label_availability_or_embargo:{index}")
        if (
            (prior_end is not None and fold.validation_start < prior_end)
            or (prior_train is not None and fold.train_through < prior_train)
            or (prior_training_labels is not None
                and fold.training_labels_available_through < prior_training_labels)
        ):
            failures.append(f"fold_temporal_order:{index}")
        if (
            not fold.training_data_hashes
            or len(set(fold.training_data_hashes)) != len(fold.training_data_hashes)
            or fold.validation_data_hash in fold.training_data_hashes
            or fold.validation_data_hash in prior_validation_labels
            or not set(fold.training_data_hashes).union({fold.validation_data_hash}) <= declared_data
        ):
            failures.append(f"fold_dataset_identity_conflict:{index}")
        for digest in set(fold.training_data_hashes) & all_validation_ids:
            available = prior_validation_labels.get(digest)
            if available is None or available > fold.training_labels_available_through:
                failures.append(f"fold_future_validation_data_reuse:{index}")
        prior_end = fold.validation_end
        prior_train = fold.train_through
        prior_training_labels = fold.training_labels_available_through
        prior_validation_labels[fold.validation_data_hash] = fold.validation_labels_available_through
    return FrozenValidationLineageReport(
        len(windows), not failures, tuple(dict.fromkeys(failures)),
    )

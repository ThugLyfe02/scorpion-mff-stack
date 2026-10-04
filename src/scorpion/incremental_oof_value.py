from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind

_ACTIONABLE = frozenset(
    {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT, EventKind.STOP}
)


class IncrementalOOFStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class IncrementalOOFPolicy:
    minimum_samples: int = 100
    minimum_folds: int = 3
    minimum_mean_log_loss_improvement: float = 0.0
    minimum_mean_brier_improvement: float = 0.0
    minimum_positive_fold_ratio: float = 0.75
    minimum_bootstrap_lower_improvement: float = 0.0
    maximum_false_action_rate_delta: float = 0.0
    maximum_wrong_action_rate_delta: float = 0.0
    bootstrap_trials: int = 2000
    lower_percentile: float = 0.10
    random_seed: int = 160037

    def __post_init__(self) -> None:
        if self.minimum_samples <= 0 or self.minimum_folds < 2:
            raise ValueError("sample/fold thresholds are invalid")
        if not 0 <= self.minimum_positive_fold_ratio <= 1:
            raise ValueError("minimum_positive_fold_ratio must be in [0,1]")
        if self.bootstrap_trials < 100:
            raise ValueError("bootstrap_trials must be >=100")
        if not 0 < self.lower_percentile < 0.5:
            raise ValueError("lower_percentile must be in (0,0.5)")


@dataclass(frozen=True, slots=True)
class IncrementalOOFExample:
    event_id: str
    fold: int
    truth: EventKind
    incumbent_probabilities: Mapping[EventKind, float]
    challenger_probabilities: Mapping[EventKind, float]


@dataclass(frozen=True, slots=True)
class IncrementalOOFFold:
    fold: int
    samples: int
    incumbent_log_loss: float
    challenger_log_loss: float
    log_loss_improvement: float
    incumbent_brier: float
    challenger_brier: float
    brier_improvement: float


@dataclass(frozen=True, slots=True)
class IncrementalOOFReport:
    samples: int
    folds: tuple[IncrementalOOFFold, ...]
    incumbent_log_loss: float
    challenger_log_loss: float
    mean_log_loss_improvement: float
    bootstrap_lower_log_loss_improvement: float
    incumbent_brier: float
    challenger_brier: float
    mean_brier_improvement: float
    positive_fold_ratio: float
    incumbent_false_action_rate: float
    challenger_false_action_rate: float
    false_action_rate_delta: float
    incumbent_wrong_action_rate: float
    challenger_wrong_action_rate: float
    wrong_action_rate_delta: float
    status: IncrementalOOFStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is IncrementalOOFStatus.QUALIFIED


def _validate(probabilities: Mapping[EventKind, float], labels: Sequence[EventKind]) -> None:
    missing = [label for label in labels if label not in probabilities]
    if missing:
        raise ValueError("probability row is missing labels")
    values = [probabilities[label] for label in labels]
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("probabilities must be in [0,1]")
    if abs(sum(values) - 1.0) > 1e-6:
        raise ValueError("probabilities must sum to 1")


def _argmax(probabilities: Mapping[EventKind, float], labels: Sequence[EventKind]) -> EventKind:
    return max(labels, key=lambda label: probabilities[label])


def _log_loss(probabilities: Mapping[EventKind, float], truth: EventKind) -> float:
    return -math.log(max(probabilities[truth], 1e-12))


def _brier(
    probabilities: Mapping[EventKind, float],
    truth: EventKind,
    labels: Sequence[EventKind],
) -> float:
    return sum(
        (probabilities[label] - float(label is truth)) ** 2
        for label in labels
    )


def _rates(
    examples: Sequence[IncrementalOOFExample],
    *,
    challenger: bool,
    labels: Sequence[EventKind],
) -> tuple[float, float]:
    false_actions = 0
    false_action_opportunities = 0
    wrong_actions = 0
    wrong_action_opportunities = 0
    for row in examples:
        probabilities = (
            row.challenger_probabilities if challenger else row.incumbent_probabilities
        )
        predicted = _argmax(probabilities, labels)
        if row.truth not in _ACTIONABLE:
            false_action_opportunities += 1
            false_actions += int(predicted in _ACTIONABLE)
        elif predicted in _ACTIONABLE:
            wrong_action_opportunities += 1
            wrong_actions += int(predicted is not row.truth)
    false_rate = (
        false_actions / false_action_opportunities if false_action_opportunities else 0.0
    )
    wrong_rate = (
        wrong_actions / wrong_action_opportunities if wrong_action_opportunities else 0.0
    )
    return false_rate, wrong_rate


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(fraction * (len(ordered) - 1))))
    return ordered[index]


def evaluate_incremental_oof_value(
    examples: Sequence[IncrementalOOFExample],
    *,
    labels: tuple[EventKind, ...],
    policy: IncrementalOOFPolicy | None = None,
) -> IncrementalOOFReport:
    """Require a challenger to add paired held-out value over the incumbent.

    This evaluates predictions that were already generated out-of-fold. It does not train or
    deploy a model. Feature families and model components therefore receive credit only for
    incremental held-out improvement, not standalone correlation or in-sample fit.
    """
    policy = policy or IncrementalOOFPolicy()
    if not labels or len(set(labels)) != len(labels):
        raise ValueError("labels must be unique and non-empty")
    ordered = sorted(examples, key=lambda item: (item.fold, item.event_id))
    for row in ordered:
        _validate(row.incumbent_probabilities, labels)
        _validate(row.challenger_probabilities, labels)
        if row.truth not in labels:
            raise ValueError("truth label is outside the evaluation label set")

    fold_ids = sorted({row.fold for row in ordered})
    fold_reports: list[IncrementalOOFFold] = []
    for fold in fold_ids:
        rows = [row for row in ordered if row.fold == fold]
        incumbent_ll = statistics.fmean(
            _log_loss(row.incumbent_probabilities, row.truth) for row in rows
        )
        challenger_ll = statistics.fmean(
            _log_loss(row.challenger_probabilities, row.truth) for row in rows
        )
        incumbent_brier = statistics.fmean(
            _brier(row.incumbent_probabilities, row.truth, labels) for row in rows
        )
        challenger_brier = statistics.fmean(
            _brier(row.challenger_probabilities, row.truth, labels) for row in rows
        )
        fold_reports.append(
            IncrementalOOFFold(
                fold=fold,
                samples=len(rows),
                incumbent_log_loss=incumbent_ll,
                challenger_log_loss=challenger_ll,
                log_loss_improvement=incumbent_ll - challenger_ll,
                incumbent_brier=incumbent_brier,
                challenger_brier=challenger_brier,
                brier_improvement=incumbent_brier - challenger_brier,
            )
        )

    samples = len(ordered)
    incumbent_ll = (
        statistics.fmean(_log_loss(row.incumbent_probabilities, row.truth) for row in ordered)
        if ordered
        else 0.0
    )
    challenger_ll = (
        statistics.fmean(_log_loss(row.challenger_probabilities, row.truth) for row in ordered)
        if ordered
        else 0.0
    )
    incumbent_brier = (
        statistics.fmean(
            _brier(row.incumbent_probabilities, row.truth, labels) for row in ordered
        )
        if ordered
        else 0.0
    )
    challenger_brier = (
        statistics.fmean(
            _brier(row.challenger_probabilities, row.truth, labels) for row in ordered
        )
        if ordered
        else 0.0
    )
    fold_improvements = [item.log_loss_improvement for item in fold_reports]
    positive_fold_ratio = (
        sum(value > 0 for value in fold_improvements) / len(fold_improvements)
        if fold_improvements
        else 0.0
    )
    rng = random.Random(policy.random_seed)
    bootstrapped: list[float] = []
    if fold_improvements:
        for _ in range(policy.bootstrap_trials):
            sampled = [
                fold_improvements[rng.randrange(len(fold_improvements))]
                for _ in fold_improvements
            ]
            bootstrapped.append(statistics.fmean(sampled))
    lower = _percentile(bootstrapped, policy.lower_percentile)
    incumbent_false, incumbent_wrong = _rates(ordered, challenger=False, labels=labels)
    challenger_false, challenger_wrong = _rates(ordered, challenger=True, labels=labels)
    false_delta = challenger_false - incumbent_false
    wrong_delta = challenger_wrong - incumbent_wrong
    mean_ll_improvement = incumbent_ll - challenger_ll
    mean_brier_improvement = incumbent_brier - challenger_brier

    failures: list[str] = []
    if samples < policy.minimum_samples:
        failures.append(f"insufficient_samples:{samples}<{policy.minimum_samples}")
    if len(fold_reports) < policy.minimum_folds:
        failures.append(
            f"insufficient_folds:{len(fold_reports)}<{policy.minimum_folds}"
        )
    if mean_ll_improvement <= policy.minimum_mean_log_loss_improvement:
        failures.append(
            "mean_log_loss_improvement_not_positive:"
            f"{mean_ll_improvement:.6f}<={policy.minimum_mean_log_loss_improvement:.6f}"
        )
    if mean_brier_improvement <= policy.minimum_mean_brier_improvement:
        failures.append(
            "mean_brier_improvement_not_positive:"
            f"{mean_brier_improvement:.6f}<={policy.minimum_mean_brier_improvement:.6f}"
        )
    if positive_fold_ratio < policy.minimum_positive_fold_ratio:
        failures.append(
            "positive_fold_ratio_below_threshold:"
            f"{positive_fold_ratio:.6f}<{policy.minimum_positive_fold_ratio:.6f}"
        )
    if lower <= policy.minimum_bootstrap_lower_improvement:
        failures.append(
            "bootstrap_lower_improvement_not_positive:"
            f"{lower:.6f}<={policy.minimum_bootstrap_lower_improvement:.6f}"
        )
    if false_delta > policy.maximum_false_action_rate_delta:
        failures.append(
            "false_action_rate_delta_above_threshold:"
            f"{false_delta:.6f}>{policy.maximum_false_action_rate_delta:.6f}"
        )
    if wrong_delta > policy.maximum_wrong_action_rate_delta:
        failures.append(
            "wrong_action_rate_delta_above_threshold:"
            f"{wrong_delta:.6f}>{policy.maximum_wrong_action_rate_delta:.6f}"
        )

    if any(item.startswith("insufficient_") for item in failures):
        status = IncrementalOOFStatus.INSUFFICIENT
    elif failures:
        status = IncrementalOOFStatus.FAILED
    else:
        status = IncrementalOOFStatus.QUALIFIED
    return IncrementalOOFReport(
        samples=samples,
        folds=tuple(fold_reports),
        incumbent_log_loss=incumbent_ll,
        challenger_log_loss=challenger_ll,
        mean_log_loss_improvement=mean_ll_improvement,
        bootstrap_lower_log_loss_improvement=lower,
        incumbent_brier=incumbent_brier,
        challenger_brier=challenger_brier,
        mean_brier_improvement=mean_brier_improvement,
        positive_fold_ratio=positive_fold_ratio,
        incumbent_false_action_rate=incumbent_false,
        challenger_false_action_rate=challenger_false,
        false_action_rate_delta=false_delta,
        incumbent_wrong_action_rate=incumbent_wrong,
        challenger_wrong_action_rate=challenger_wrong,
        wrong_action_rate_delta=wrong_delta,
        status=status,
        failures=tuple(failures),
    )

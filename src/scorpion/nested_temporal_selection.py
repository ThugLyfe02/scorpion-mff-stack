from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class NestedSelectionStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class CandidateFoldObservation:
    candidate_id: str
    fold: int
    reward: float
    false_action_rate: float = 0.0
    wrong_action_rate: float = 0.0

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id is required")
        if self.fold < 0:
            raise ValueError("fold cannot be negative")
        for name in ("false_action_rate", "wrong_action_rate"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")


@dataclass(frozen=True, slots=True)
class NestedSelectionPolicy:
    minimum_candidates: int = 2
    minimum_folds: int = 4
    minimum_training_folds: int = 2
    minimum_oos_folds: int = 2
    minimum_mean_oos_reward: float = 0.0
    minimum_positive_fold_ratio: float = 0.60
    maximum_false_action_rate: float = 0.005
    maximum_wrong_action_rate: float = 0.01
    maximum_mean_oracle_regret: float = 0.05
    maximum_switch_rate: float = 0.80

    def __post_init__(self) -> None:
        if self.minimum_candidates < 2:
            raise ValueError("minimum_candidates must be >=2")
        if self.minimum_folds < 3 or self.minimum_training_folds < 1:
            raise ValueError("fold thresholds are invalid")
        if self.minimum_oos_folds <= 0:
            raise ValueError("minimum_oos_folds must be positive")
        if self.minimum_training_folds >= self.minimum_folds:
            raise ValueError("minimum_training_folds must be < minimum_folds")
        for name in (
            "minimum_positive_fold_ratio",
            "maximum_false_action_rate",
            "maximum_wrong_action_rate",
            "maximum_switch_rate",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        if self.maximum_mean_oracle_regret < 0:
            raise ValueError("maximum_mean_oracle_regret cannot be negative")


@dataclass(frozen=True, slots=True)
class NestedFoldSelection:
    fold: int
    training_folds: tuple[int, ...]
    selected_candidate: str
    training_mean_reward: float
    oos_reward: float
    oracle_candidate: str
    oracle_reward: float
    oracle_regret: float
    false_action_rate: float
    wrong_action_rate: float


@dataclass(frozen=True, slots=True)
class NestedSelectionReport:
    candidates: tuple[str, ...]
    folds: tuple[int, ...]
    selections: tuple[NestedFoldSelection, ...]
    mean_oos_reward: float
    positive_fold_ratio: float
    mean_oracle_regret: float
    switch_rate: float
    selected_false_action_rate: float
    selected_wrong_action_rate: float
    selection_counts: tuple[tuple[str, int], ...]
    status: NestedSelectionStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is NestedSelectionStatus.QUALIFIED


def _index(
    observations: Sequence[CandidateFoldObservation],
) -> tuple[tuple[str, ...], tuple[int, ...], dict[tuple[str, int], CandidateFoldObservation]]:
    candidates = tuple(sorted({item.candidate_id for item in observations}))
    folds = tuple(sorted({item.fold for item in observations}))
    indexed: dict[tuple[str, int], CandidateFoldObservation] = {}
    for item in observations:
        key = (item.candidate_id, item.fold)
        if key in indexed:
            raise ValueError(f"duplicate candidate/fold observation: {key}")
        indexed[key] = item
    for candidate in candidates:
        missing = [fold for fold in folds if (candidate, fold) not in indexed]
        if missing:
            raise ValueError(f"candidate {candidate} is missing folds {missing}")
    return candidates, folds, indexed


def _safe_training_candidate(
    candidate: str,
    training_folds: tuple[int, ...],
    indexed: dict[tuple[str, int], CandidateFoldObservation],
    policy: NestedSelectionPolicy,
) -> tuple[bool, float]:
    rows = [indexed[(candidate, fold)] for fold in training_folds]
    mean_false = statistics.fmean(item.false_action_rate for item in rows)
    mean_wrong = statistics.fmean(item.wrong_action_rate for item in rows)
    mean_reward = statistics.fmean(item.reward for item in rows)
    safe = (
        mean_false <= policy.maximum_false_action_rate
        and mean_wrong <= policy.maximum_wrong_action_rate
    )
    return safe, mean_reward


def evaluate_nested_temporal_selection(
    observations: Sequence[CandidateFoldObservation],
    *,
    policy: NestedSelectionPolicy | None = None,
) -> NestedSelectionReport:
    """Evaluate whether the *candidate selection procedure* works out of sample.

    Candidate ranking for fold ``t`` may use only folds strictly before ``t``. The current fold
    is used only after a candidate is selected. Oracle performance is diagnostic and never enters
    the selection rule. This component is research-only and cannot activate a candidate.
    """
    policy = policy or NestedSelectionPolicy()
    candidates, folds, indexed = _index(observations)
    failures: list[str] = []
    if len(candidates) < policy.minimum_candidates:
        failures.append(
            f"insufficient_candidates:{len(candidates)}<{policy.minimum_candidates}"
        )
    if len(folds) < policy.minimum_folds:
        failures.append(f"insufficient_folds:{len(folds)}<{policy.minimum_folds}")
    if failures:
        return NestedSelectionReport(
            candidates=candidates,
            folds=folds,
            selections=(),
            mean_oos_reward=0.0,
            positive_fold_ratio=0.0,
            mean_oracle_regret=0.0,
            switch_rate=0.0,
            selected_false_action_rate=0.0,
            selected_wrong_action_rate=0.0,
            selection_counts=(),
            status=NestedSelectionStatus.INSUFFICIENT,
            failures=tuple(failures),
        )

    selections: list[NestedFoldSelection] = []
    for position in range(policy.minimum_training_folds, len(folds)):
        fold = folds[position]
        training_folds = folds[:position]
        ranked: list[tuple[float, str]] = []
        for candidate in candidates:
            safe, mean_reward = _safe_training_candidate(
                candidate,
                training_folds,
                indexed,
                policy,
            )
            if safe:
                ranked.append((mean_reward, candidate))
        if not ranked:
            failures.append(f"no_safe_candidate_for_fold:{fold}")
            continue
        training_reward, selected = max(ranked, key=lambda item: (item[0], item[1]))
        selected_row = indexed[(selected, fold)]
        oracle_row = max(
            (indexed[(candidate, fold)] for candidate in candidates),
            key=lambda item: (item.reward, item.candidate_id),
        )
        selections.append(
            NestedFoldSelection(
                fold=fold,
                training_folds=training_folds,
                selected_candidate=selected,
                training_mean_reward=training_reward,
                oos_reward=selected_row.reward,
                oracle_candidate=oracle_row.candidate_id,
                oracle_reward=oracle_row.reward,
                oracle_regret=max(0.0, oracle_row.reward - selected_row.reward),
                false_action_rate=selected_row.false_action_rate,
                wrong_action_rate=selected_row.wrong_action_rate,
            )
        )

    if len(selections) < policy.minimum_oos_folds:
        failures.append(
            f"insufficient_oos_folds:{len(selections)}<{policy.minimum_oos_folds}"
        )
    rewards = [item.oos_reward for item in selections]
    mean_reward = statistics.fmean(rewards) if rewards else 0.0
    positive_ratio = sum(value > 0 for value in rewards) / len(rewards) if rewards else 0.0
    mean_regret = (
        statistics.fmean(item.oracle_regret for item in selections) if selections else 0.0
    )
    false_rate = (
        statistics.fmean(item.false_action_rate for item in selections) if selections else 0.0
    )
    wrong_rate = (
        statistics.fmean(item.wrong_action_rate for item in selections) if selections else 0.0
    )
    switches = sum(
        left.selected_candidate != right.selected_candidate
        for left, right in zip(selections, selections[1:], strict=False)
    )
    switch_rate = switches / max(1, len(selections) - 1) if selections else 0.0

    if selections and mean_reward <= policy.minimum_mean_oos_reward:
        failures.append(
            "nested_mean_oos_reward_not_positive:"
            f"{mean_reward:.6f}<={policy.minimum_mean_oos_reward:.6f}"
        )
    if selections and positive_ratio < policy.minimum_positive_fold_ratio:
        failures.append(
            "nested_positive_fold_ratio_below_threshold:"
            f"{positive_ratio:.6f}<{policy.minimum_positive_fold_ratio:.6f}"
        )
    if mean_regret > policy.maximum_mean_oracle_regret:
        failures.append(
            "nested_mean_oracle_regret_above_threshold:"
            f"{mean_regret:.6f}>{policy.maximum_mean_oracle_regret:.6f}"
        )
    if false_rate > policy.maximum_false_action_rate:
        failures.append(
            "nested_false_action_rate_above_threshold:"
            f"{false_rate:.6f}>{policy.maximum_false_action_rate:.6f}"
        )
    if wrong_rate > policy.maximum_wrong_action_rate:
        failures.append(
            "nested_wrong_action_rate_above_threshold:"
            f"{wrong_rate:.6f}>{policy.maximum_wrong_action_rate:.6f}"
        )
    if switch_rate > policy.maximum_switch_rate:
        failures.append(
            "nested_selection_switch_rate_above_threshold:"
            f"{switch_rate:.6f}>{policy.maximum_switch_rate:.6f}"
        )

    if any(item.startswith("insufficient_") for item in failures):
        status = NestedSelectionStatus.INSUFFICIENT
    elif failures:
        status = NestedSelectionStatus.FAILED
    else:
        status = NestedSelectionStatus.QUALIFIED
    counts = Counter(item.selected_candidate for item in selections)
    return NestedSelectionReport(
        candidates=candidates,
        folds=folds,
        selections=tuple(selections),
        mean_oos_reward=mean_reward,
        positive_fold_ratio=positive_ratio,
        mean_oracle_regret=mean_regret,
        switch_rate=switch_rate,
        selected_false_action_rate=false_rate,
        selected_wrong_action_rate=wrong_rate,
        selection_counts=tuple(sorted(counts.items())),
        status=status,
        failures=tuple(failures),
    )

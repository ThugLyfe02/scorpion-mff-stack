from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind
from .incremental_oof_value import (
    IncrementalOOFExample,
    IncrementalOOFPolicy,
    IncrementalOOFReport,
    evaluate_incremental_oof_value,
)


class FeatureFamilyStatus(StrEnum):
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class FeatureFamilyCandidate:
    family_id: str
    examples: tuple[IncrementalOOFExample, ...]

    def __post_init__(self) -> None:
        if not self.family_id.strip():
            raise ValueError("family_id is required")


@dataclass(frozen=True, slots=True)
class FeatureFamilySelectionPolicy:
    false_discovery_rate: float = 0.05
    minimum_folds_for_sign_test: int = 4
    incremental_policy: IncrementalOOFPolicy = IncrementalOOFPolicy()

    def __post_init__(self) -> None:
        if not 0 < self.false_discovery_rate < 1:
            raise ValueError("false_discovery_rate must be in (0,1)")
        if self.minimum_folds_for_sign_test < 3:
            raise ValueError("minimum_folds_for_sign_test must be >=3")


@dataclass(frozen=True, slots=True)
class FeatureFamilyEvidence:
    family_id: str
    incremental: IncrementalOOFReport
    positive_fold_count: int
    tested_folds: int
    one_sided_sign_p_value: float
    fdr_q_value: float
    status: FeatureFamilyStatus
    failures: tuple[str, ...]

    @property
    def selected(self) -> bool:
        return self.status is FeatureFamilyStatus.SELECTED


@dataclass(frozen=True, slots=True)
class FeatureFamilySelectionReport:
    families_tested: int
    selected_families: tuple[str, ...]
    evidence: tuple[FeatureFamilyEvidence, ...]
    false_discovery_rate: float


def _one_sided_sign_p_value(successes: int, trials: int) -> float:
    if trials <= 0:
        return 1.0
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between zero and trials")
    return min(
        1.0,
        sum(math.comb(trials, k) for k in range(successes, trials + 1)) / (2**trials),
    )


def _bh_q_values(rows: list[tuple[str, float]]) -> dict[str, float]:
    if not rows:
        return {}
    ordered = sorted(rows, key=lambda item: (item[1], item[0]))
    count = len(ordered)
    adjusted = [0.0] * count
    running = 1.0
    for index in range(count - 1, -1, -1):
        _, p_value = ordered[index]
        rank = index + 1
        running = min(running, p_value * count / rank)
        adjusted[index] = min(1.0, running)
    return {family_id: adjusted[index] for index, (family_id, _) in enumerate(ordered)}


def evaluate_feature_families(
    candidates: tuple[FeatureFamilyCandidate, ...],
    *,
    labels: tuple[EventKind, ...],
    policy: FeatureFamilySelectionPolicy | None = None,
) -> FeatureFamilySelectionReport:
    """Select feature families only when incremental OOF value survives family search.

    Every family is evaluated against the same incumbent through the existing paired OOF gate.
    Fold-level log-loss improvements then receive a one-sided exact sign test, and those p-values
    are adjusted across *all* families tried with Benjamini-Hochberg FDR control. This prevents
    feature mining from turning repeated experimentation into unearned confidence.
    """
    policy = policy or FeatureFamilySelectionPolicy()
    if len({item.family_id for item in candidates}) != len(candidates):
        raise ValueError("family_id values must be unique")

    reports: dict[str, IncrementalOOFReport] = {}
    sign_stats: dict[str, tuple[int, int, float]] = {}
    p_values: list[tuple[str, float]] = []
    for candidate in candidates:
        report = evaluate_incremental_oof_value(
            candidate.examples,
            labels=labels,
            policy=policy.incremental_policy,
        )
        reports[candidate.family_id] = report
        fold_improvements = [item.log_loss_improvement for item in report.folds]
        successes = sum(value > 0 for value in fold_improvements)
        trials = len(fold_improvements)
        p_value = (
            _one_sided_sign_p_value(successes, trials)
            if trials >= policy.minimum_folds_for_sign_test
            else 1.0
        )
        sign_stats[candidate.family_id] = (successes, trials, p_value)
        p_values.append((candidate.family_id, p_value))

    q_values = _bh_q_values(p_values)
    evidence: list[FeatureFamilyEvidence] = []
    for family_id in sorted(reports):
        report = reports[family_id]
        successes, trials, p_value = sign_stats[family_id]
        q_value = q_values[family_id]
        failures: list[str] = []
        if trials < policy.minimum_folds_for_sign_test:
            failures.append(
                f"insufficient_sign_test_folds:{trials}<{policy.minimum_folds_for_sign_test}"
            )
        if not report.qualified:
            failures.append("incremental_oof_not_qualified")
            failures.extend(f"incremental:{item}" for item in report.failures)
        if q_value > policy.false_discovery_rate:
            failures.append(
                "feature_family_fdr_not_significant:"
                f"{q_value:.6f}>{policy.false_discovery_rate:.6f}"
            )
        if any(item.startswith("insufficient_") for item in failures):
            status = FeatureFamilyStatus.INSUFFICIENT
        elif failures:
            status = FeatureFamilyStatus.REJECTED
        else:
            status = FeatureFamilyStatus.SELECTED
        evidence.append(
            FeatureFamilyEvidence(
                family_id=family_id,
                incremental=report,
                positive_fold_count=successes,
                tested_folds=trials,
                one_sided_sign_p_value=p_value,
                fdr_q_value=q_value,
                status=status,
                failures=tuple(failures),
            )
        )

    return FeatureFamilySelectionReport(
        families_tested=len(candidates),
        selected_families=tuple(item.family_id for item in evidence if item.selected),
        evidence=tuple(evidence),
        false_discovery_rate=policy.false_discovery_rate,
    )

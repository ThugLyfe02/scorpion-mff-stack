from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind

_ACTIONABLE = frozenset(
    {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT, EventKind.STOP}
)


class RegimeMixtureStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class RegimeMixturePolicy:
    minimum_folds: int = 3
    minimum_training_samples: int = 60
    minimum_regime_training_samples: int = 25
    minimum_oos_samples: int = 100
    learning_rate: float = 0.20
    wrong_action_penalty: float = 5.0
    maximum_false_action_rate: float = 0.005
    maximum_wrong_action_rate: float = 0.01
    minimum_oos_accuracy: float = 0.90

    def __post_init__(self) -> None:
        if self.minimum_folds < 2:
            raise ValueError("minimum_folds must be >=2")
        if min(
            self.minimum_training_samples,
            self.minimum_regime_training_samples,
            self.minimum_oos_samples,
        ) <= 0:
            raise ValueError("sample thresholds must be positive")
        if self.learning_rate <= 0:
            raise ValueError("learning_rate must be positive")
        if self.wrong_action_penalty < 0:
            raise ValueError("wrong_action_penalty cannot be negative")
        if not 0 <= self.maximum_false_action_rate <= 1:
            raise ValueError("maximum_false_action_rate must be in [0,1]")
        if not 0 <= self.maximum_wrong_action_rate <= 1:
            raise ValueError("maximum_wrong_action_rate must be in [0,1]")
        if not 0 <= self.minimum_oos_accuracy <= 1:
            raise ValueError("minimum_oos_accuracy must be in [0,1]")


@dataclass(frozen=True, slots=True)
class RegimeExpertExample:
    event_id: str
    fold: int
    regime: str
    truth: EventKind
    model_probabilities: Mapping[str, Mapping[EventKind, float]]


@dataclass(frozen=True, slots=True)
class RegimeFoldResult:
    fold: int
    training_samples: int
    evaluation_samples: int
    regime_specific_predictions: int
    fallback_predictions: int
    accuracy: float
    false_action_rate: float
    wrong_action_rate: float


@dataclass(frozen=True, slots=True)
class RegimePerformance:
    regime: str
    samples: int
    accuracy: float
    false_action_rate: float
    wrong_action_rate: float


@dataclass(frozen=True, slots=True)
class RegimeMixtureReport:
    models: tuple[str, ...]
    regimes: tuple[str, ...]
    folds: tuple[RegimeFoldResult, ...]
    regime_performance: tuple[RegimePerformance, ...]
    oos_samples: int
    oos_accuracy: float
    false_action_rate: float
    wrong_action_rate: float
    fallback_rate: float
    status: RegimeMixtureStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is RegimeMixtureStatus.QUALIFIED


def _validate_distribution(
    probabilities: Mapping[EventKind, float],
    labels: Sequence[EventKind],
) -> None:
    if any(label not in probabilities for label in labels):
        raise ValueError("probability distribution is missing required labels")
    values = [probabilities[label] for label in labels]
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("probabilities must be in [0,1]")
    if abs(sum(values) - 1.0) > 1e-6:
        raise ValueError("probabilities must sum to 1")


def _model_ids(examples: Sequence[RegimeExpertExample]) -> tuple[str, ...]:
    if not examples:
        return ()
    expected = tuple(sorted(examples[0].model_probabilities))
    if not expected:
        raise ValueError("examples must contain at least one model")
    for example in examples:
        if tuple(sorted(example.model_probabilities)) != expected:
            raise ValueError("all examples must contain the same model ids")
    return expected


def _penalized_loss(
    probabilities: Mapping[EventKind, float],
    truth: EventKind,
    labels: tuple[EventKind, ...],
    policy: RegimeMixturePolicy,
) -> float:
    _validate_distribution(probabilities, labels)
    loss = -math.log(max(probabilities[truth], 1e-12))
    if truth in _ACTIONABLE:
        wrong_mass = sum(
            probabilities[label]
            for label in labels
            if label in _ACTIONABLE and label is not truth
        )
        loss += policy.wrong_action_penalty * wrong_mass
    else:
        false_mass = sum(
            probabilities[label] for label in labels if label in _ACTIONABLE
        )
        loss += policy.wrong_action_penalty * false_mass
    return loss


def _fit_weights(
    examples: Sequence[RegimeExpertExample],
    model_ids: tuple[str, ...],
    labels: tuple[EventKind, ...],
    policy: RegimeMixturePolicy,
) -> dict[str, float]:
    losses = {model_id: 0.0 for model_id in model_ids}
    for example in examples:
        for model_id in model_ids:
            losses[model_id] += _penalized_loss(
                example.model_probabilities[model_id],
                example.truth,
                labels,
                policy,
            )
    scaled = {
        model_id: -policy.learning_rate * losses[model_id] / max(len(examples), 1)
        for model_id in model_ids
    }
    maximum = max(scaled.values())
    raw = {model_id: math.exp(value - maximum) for model_id, value in scaled.items()}
    total = sum(raw.values())
    return {model_id: value / total for model_id, value in raw.items()}


def _mixture(
    example: RegimeExpertExample,
    weights: Mapping[str, float],
    labels: tuple[EventKind, ...],
) -> dict[EventKind, float]:
    output = {label: 0.0 for label in labels}
    for model_id, weight in weights.items():
        probabilities = example.model_probabilities[model_id]
        _validate_distribution(probabilities, labels)
        for label in labels:
            output[label] += weight * probabilities[label]
    return output


def _prediction_metrics(
    rows: Sequence[tuple[RegimeExpertExample, EventKind]],
) -> tuple[float, float, float]:
    if not rows:
        return 0.0, 0.0, 0.0
    correct = 0
    false_actions = 0
    wrong_actions = 0
    for example, prediction in rows:
        correct += int(prediction is example.truth)
        truth_actionable = example.truth in _ACTIONABLE
        prediction_actionable = prediction in _ACTIONABLE
        false_actions += int(not truth_actionable and prediction_actionable)
        wrong_actions += int(
            truth_actionable and prediction_actionable and prediction is not example.truth
        )
    total = len(rows)
    return correct / total, false_actions / total, wrong_actions / total


def evaluate_regime_mixture(
    examples: Sequence[RegimeExpertExample],
    *,
    labels: tuple[EventKind, ...],
    policy: RegimeMixturePolicy | None = None,
) -> RegimeMixtureReport:
    """Evaluate a regime-conditioned shadow ensemble with strict temporal training order."""
    policy = policy or RegimeMixturePolicy()
    ordered = sorted(examples, key=lambda item: (item.fold, item.event_id))
    model_ids = _model_ids(ordered)
    folds = sorted({item.fold for item in ordered})
    regimes = tuple(sorted({item.regime for item in ordered}))
    if len(folds) < policy.minimum_folds or not model_ids:
        return RegimeMixtureReport(
            models=model_ids,
            regimes=regimes,
            folds=(),
            regime_performance=(),
            oos_samples=0,
            oos_accuracy=0.0,
            false_action_rate=0.0,
            wrong_action_rate=0.0,
            fallback_rate=0.0,
            status=RegimeMixtureStatus.INSUFFICIENT,
            failures=(f"insufficient_folds:{len(folds)}<{policy.minimum_folds}",),
        )

    evaluated: list[tuple[RegimeExpertExample, EventKind, bool]] = []
    fold_results: list[RegimeFoldResult] = []
    for fold in folds[1:]:
        training = [item for item in ordered if item.fold < fold]
        evaluation = [item for item in ordered if item.fold == fold]
        if len(training) < policy.minimum_training_samples or not evaluation:
            continue
        global_weights = _fit_weights(training, model_ids, labels, policy)
        by_regime: dict[str, list[RegimeExpertExample]] = {}
        for item in training:
            by_regime.setdefault(item.regime, []).append(item)
        regime_weights = {
            regime: _fit_weights(rows, model_ids, labels, policy)
            for regime, rows in by_regime.items()
            if len(rows) >= policy.minimum_regime_training_samples
        }
        fold_predictions: list[tuple[RegimeExpertExample, EventKind]] = []
        regime_specific = 0
        fallback = 0
        for item in evaluation:
            weights = regime_weights.get(item.regime)
            used_regime = weights is not None
            if weights is None:
                weights = global_weights
                fallback += 1
            else:
                regime_specific += 1
            probabilities = _mixture(item, weights, labels)
            prediction = max(labels, key=lambda label: probabilities[label])
            fold_predictions.append((item, prediction))
            evaluated.append((item, prediction, used_regime))
        accuracy, false_rate, wrong_rate = _prediction_metrics(fold_predictions)
        fold_results.append(
            RegimeFoldResult(
                fold=fold,
                training_samples=len(training),
                evaluation_samples=len(evaluation),
                regime_specific_predictions=regime_specific,
                fallback_predictions=fallback,
                accuracy=accuracy,
                false_action_rate=false_rate,
                wrong_action_rate=wrong_rate,
            )
        )

    simple_rows = [(item, prediction) for item, prediction, _ in evaluated]
    accuracy, false_rate, wrong_rate = _prediction_metrics(simple_rows)
    regime_reports: list[RegimePerformance] = []
    for regime in regimes:
        rows = [(item, prediction) for item, prediction, _ in evaluated if item.regime == regime]
        if not rows:
            continue
        regime_accuracy, regime_false, regime_wrong = _prediction_metrics(rows)
        regime_reports.append(
            RegimePerformance(
                regime=regime,
                samples=len(rows),
                accuracy=regime_accuracy,
                false_action_rate=regime_false,
                wrong_action_rate=regime_wrong,
            )
        )
    fallback_rate = (
        sum(not used_regime for _, _, used_regime in evaluated) / len(evaluated)
        if evaluated
        else 0.0
    )
    failures: list[str] = []
    if len(evaluated) < policy.minimum_oos_samples:
        failures.append(
            f"insufficient_oos_samples:{len(evaluated)}<{policy.minimum_oos_samples}"
        )
    if len(evaluated) >= policy.minimum_oos_samples and accuracy < policy.minimum_oos_accuracy:
        failures.append(
            f"oos_accuracy_below_threshold:{accuracy:.6f}<{policy.minimum_oos_accuracy:.6f}"
        )
    if false_rate > policy.maximum_false_action_rate:
        failures.append(
            f"false_action_rate_above_threshold:{false_rate:.6f}>"
            f"{policy.maximum_false_action_rate:.6f}"
        )
    if wrong_rate > policy.maximum_wrong_action_rate:
        failures.append(
            f"wrong_action_rate_above_threshold:{wrong_rate:.6f}>"
            f"{policy.maximum_wrong_action_rate:.6f}"
        )
    if any(item.startswith("insufficient_") for item in failures):
        status = RegimeMixtureStatus.INSUFFICIENT
    elif failures:
        status = RegimeMixtureStatus.FAILED
    else:
        status = RegimeMixtureStatus.QUALIFIED
    return RegimeMixtureReport(
        models=model_ids,
        regimes=regimes,
        folds=tuple(fold_results),
        regime_performance=tuple(regime_reports),
        oos_samples=len(evaluated),
        oos_accuracy=accuracy,
        false_action_rate=false_rate,
        wrong_action_rate=wrong_rate,
        fallback_rate=fallback_rate,
        status=status,
        failures=tuple(failures),
    )

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind

_ACTIONABLE = frozenset(
    {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT, EventKind.STOP}
)


class StackingStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class StackingPolicy:
    minimum_folds: int = 3
    minimum_training_samples: int = 50
    minimum_oos_samples: int = 100
    iterations: int = 250
    learning_rate: float = 0.05
    wrong_action_penalty: float = 4.0
    maximum_false_action_rate: float = 0.005
    maximum_wrong_action_rate: float = 0.01

    def __post_init__(self) -> None:
        if self.minimum_folds < 2:
            raise ValueError("minimum_folds must be >=2")
        if self.minimum_training_samples <= 0 or self.minimum_oos_samples <= 0:
            raise ValueError("sample thresholds must be positive")
        if self.iterations <= 0 or self.learning_rate <= 0:
            raise ValueError("iterations and learning_rate must be positive")
        if self.wrong_action_penalty < 0:
            raise ValueError("wrong_action_penalty cannot be negative")
        for name in ("maximum_false_action_rate", "maximum_wrong_action_rate"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")


@dataclass(frozen=True, slots=True)
class StackingExample:
    event_id: str
    fold: int
    truth: EventKind
    model_probabilities: Mapping[str, Mapping[EventKind, float]]


@dataclass(frozen=True, slots=True)
class FoldStackingResult:
    fold: int
    training_samples: int
    evaluation_samples: int
    weights: tuple[tuple[str, float], ...]
    accuracy: float
    log_loss: float
    brier: float
    false_action_rate: float
    wrong_action_rate: float


@dataclass(frozen=True, slots=True)
class CrossFittedStackingReport:
    models: tuple[str, ...]
    folds: tuple[FoldStackingResult, ...]
    oos_samples: int
    oos_accuracy: float
    oos_log_loss: float
    oos_brier: float
    false_action_rate: float
    wrong_action_rate: float
    final_weights: tuple[tuple[str, float], ...]
    status: StackingStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is StackingStatus.QUALIFIED


def _validate_distribution(
    probabilities: Mapping[EventKind, float],
    labels: Sequence[EventKind],
) -> None:
    missing = [label for label in labels if label not in probabilities]
    if missing:
        raise ValueError("probability distribution is missing required labels")
    values = [probabilities[label] for label in labels]
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("probabilities must be in [0,1]")
    if abs(sum(values) - 1.0) > 1e-6:
        raise ValueError("probabilities must sum to 1")


def _model_ids(examples: Sequence[StackingExample]) -> tuple[str, ...]:
    if not examples:
        return ()
    expected = tuple(sorted(examples[0].model_probabilities))
    if not expected:
        raise ValueError("stacking example has no model probabilities")
    for example in examples:
        if tuple(sorted(example.model_probabilities)) != expected:
            raise ValueError("all stacking examples must contain the same model ids")
    return expected


def _normalize(weights: dict[str, float]) -> None:
    total = sum(weights.values())
    if total <= 0:
        uniform = 1.0 / len(weights)
        for model_id in weights:
            weights[model_id] = uniform
        return
    for model_id in weights:
        weights[model_id] /= total


def _mixture(
    example: StackingExample,
    weights: Mapping[str, float],
    labels: Sequence[EventKind],
) -> dict[EventKind, float]:
    output = {label: 0.0 for label in labels}
    for model_id, weight in weights.items():
        probabilities = example.model_probabilities[model_id]
        _validate_distribution(probabilities, labels)
        for label in labels:
            output[label] += weight * probabilities[label]
    return output


def _fit_weights(
    examples: Sequence[StackingExample],
    model_ids: tuple[str, ...],
    labels: tuple[EventKind, ...],
    policy: StackingPolicy,
) -> dict[str, float]:
    weights = {model_id: 1.0 / len(model_ids) for model_id in model_ids}
    for _ in range(policy.iterations):
        gradients = {model_id: 0.0 for model_id in model_ids}
        for example in examples:
            mixture = _mixture(example, weights, labels)
            truth_probability = max(mixture[example.truth], 1e-12)
            truth_actionable = example.truth in _ACTIONABLE
            for model_id in model_ids:
                probabilities = example.model_probabilities[model_id]
                action_mass = sum(
                    probabilities[label] for label in labels if label in _ACTIONABLE
                )
                wrong_action_mass = (
                    action_mass - probabilities[example.truth]
                    if truth_actionable
                    else action_mass
                )
                gradients[model_id] += (
                    -probabilities[example.truth] / truth_probability
                    + policy.wrong_action_penalty * wrong_action_mass
                )
        scale = 1.0 / max(len(examples), 1)
        for model_id in model_ids:
            exponent = -policy.learning_rate * gradients[model_id] * scale
            weights[model_id] *= math.exp(max(-20.0, min(20.0, exponent)))
        _normalize(weights)
    return weights


def _metrics(
    examples: Sequence[StackingExample],
    weights: Mapping[str, float],
    labels: tuple[EventKind, ...],
) -> tuple[float, float, float, float, float]:
    if not examples:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    correct = 0
    log_losses: list[float] = []
    briers: list[float] = []
    false_actions = 0
    wrong_actions = 0
    for example in examples:
        mixture = _mixture(example, weights, labels)
        predicted = max(labels, key=lambda label: mixture[label])
        correct += int(predicted is example.truth)
        log_losses.append(-math.log(max(mixture[example.truth], 1e-12)))
        briers.append(
            sum(
                (mixture[label] - (1.0 if label is example.truth else 0.0)) ** 2
                for label in labels
            )
        )
        if predicted in _ACTIONABLE and example.truth not in _ACTIONABLE:
            false_actions += 1
        if (
            predicted in _ACTIONABLE
            and example.truth in _ACTIONABLE
            and predicted is not example.truth
        ):
            wrong_actions += 1
    count = len(examples)
    return (
        correct / count,
        sum(log_losses) / count,
        sum(briers) / count,
        false_actions / count,
        wrong_actions / count,
    )


def evaluate_cross_fitted_stacking(
    examples: Sequence[StackingExample],
    *,
    labels: tuple[EventKind, ...],
    policy: StackingPolicy | None = None,
) -> CrossFittedStackingReport:
    policy = policy or StackingPolicy()
    if len(set(labels)) != len(labels) or not labels:
        raise ValueError("labels must be unique and non-empty")
    model_ids = _model_ids(examples)
    if not model_ids:
        return CrossFittedStackingReport(
            (), (), 0, 0.0, 0.0, 0.0, 0.0, 0.0, (),
            StackingStatus.INSUFFICIENT,
            ("no_stacking_examples",),
        )
    for example in examples:
        if example.truth not in labels:
            raise ValueError("truth label missing from stacking label space")
        for probabilities in example.model_probabilities.values():
            _validate_distribution(probabilities, labels)

    folds = sorted({example.fold for example in examples})
    if len(folds) < policy.minimum_folds:
        return CrossFittedStackingReport(
            model_ids, (), 0, 0.0, 0.0, 0.0, 0.0, 0.0,
            tuple((model_id, 1.0 / len(model_ids)) for model_id in model_ids),
            StackingStatus.INSUFFICIENT,
            (f"insufficient_folds:{len(folds)}<{policy.minimum_folds}",),
        )

    fold_results: list[FoldStackingResult] = []
    pooled_oos: list[tuple[StackingExample, dict[str, float]]] = []
    for fold in folds:
        training = [example for example in examples if example.fold < fold]
        evaluation = [example for example in examples if example.fold == fold]
        if len(training) < policy.minimum_training_samples or not evaluation:
            continue
        weights = _fit_weights(training, model_ids, labels, policy)
        accuracy, log_loss, brier, false_action, wrong_action = _metrics(
            evaluation, weights, labels
        )
        fold_results.append(
            FoldStackingResult(
                fold=fold,
                training_samples=len(training),
                evaluation_samples=len(evaluation),
                weights=tuple(sorted(weights.items())),
                accuracy=accuracy,
                log_loss=log_loss,
                brier=brier,
                false_action_rate=false_action,
                wrong_action_rate=wrong_action,
            )
        )
        pooled_oos.extend((example, dict(weights)) for example in evaluation)

    if not pooled_oos:
        return CrossFittedStackingReport(
            model_ids, tuple(fold_results), 0, 0.0, 0.0, 0.0, 0.0, 0.0,
            tuple((model_id, 1.0 / len(model_ids)) for model_id in model_ids),
            StackingStatus.INSUFFICIENT,
            ("no_eligible_out_of_fold_evaluation",),
        )

    correct = 0
    log_loss_sum = 0.0
    brier_sum = 0.0
    false_actions = 0
    wrong_actions = 0
    for example, weights in pooled_oos:
        mixture = _mixture(example, weights, labels)
        predicted = max(labels, key=lambda label: mixture[label])
        correct += int(predicted is example.truth)
        log_loss_sum += -math.log(max(mixture[example.truth], 1e-12))
        brier_sum += sum(
            (mixture[label] - (1.0 if label is example.truth else 0.0)) ** 2
            for label in labels
        )
        false_actions += int(predicted in _ACTIONABLE and example.truth not in _ACTIONABLE)
        wrong_actions += int(
            predicted in _ACTIONABLE
            and example.truth in _ACTIONABLE
            and predicted is not example.truth
        )
    oos_samples = len(pooled_oos)
    false_action_rate = false_actions / oos_samples
    wrong_action_rate = wrong_actions / oos_samples
    failures: list[str] = []
    if oos_samples < policy.minimum_oos_samples:
        failures.append(f"insufficient_oos_samples:{oos_samples}<{policy.minimum_oos_samples}")
    if false_action_rate > policy.maximum_false_action_rate:
        failures.append(
            f"false_action_rate:{false_action_rate:.6f}>{policy.maximum_false_action_rate:.6f}"
        )
    if wrong_action_rate > policy.maximum_wrong_action_rate:
        failures.append(
            f"wrong_action_rate:{wrong_action_rate:.6f}>{policy.maximum_wrong_action_rate:.6f}"
        )
    status = (
        StackingStatus.INSUFFICIENT
        if any(item.startswith("insufficient_") for item in failures)
        else StackingStatus.FAILED if failures else StackingStatus.QUALIFIED
    )
    all_training = list(examples)
    final_weights = _fit_weights(all_training, model_ids, labels, policy)
    return CrossFittedStackingReport(
        models=model_ids,
        folds=tuple(fold_results),
        oos_samples=oos_samples,
        oos_accuracy=correct / oos_samples,
        oos_log_loss=log_loss_sum / oos_samples,
        oos_brier=brier_sum / oos_samples,
        false_action_rate=false_action_rate,
        wrong_action_rate=wrong_action_rate,
        final_weights=tuple(sorted(final_weights.items())),
        status=status,
        failures=tuple(failures),
    )

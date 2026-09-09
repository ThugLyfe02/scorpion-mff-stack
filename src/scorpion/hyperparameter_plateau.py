from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class PlateauStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class HyperparameterObservation:
    config_id: str
    fold: int
    parameters: Mapping[str, float]
    reward: float
    false_action_rate: float = 0.0
    wrong_action_rate: float = 0.0

    def __post_init__(self) -> None:
        if not self.config_id.strip():
            raise ValueError("config_id is required")
        if self.fold < 0:
            raise ValueError("fold cannot be negative")
        if not self.parameters:
            raise ValueError("parameters cannot be empty")
        if any(not math.isfinite(value) for value in self.parameters.values()):
            raise ValueError("parameter values must be finite")
        for name in ("false_action_rate", "wrong_action_rate"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")


@dataclass(frozen=True, slots=True)
class HyperparameterPlateauPolicy:
    minimum_configs: int = 5
    minimum_folds: int = 3
    neighborhood_radius: float = 0.30
    reward_retention_ratio: float = 0.90
    minimum_neighbors: int = 2
    minimum_plateau_neighbor_ratio: float = 0.60
    maximum_false_action_rate: float = 0.005
    maximum_wrong_action_rate: float = 0.01

    def __post_init__(self) -> None:
        if self.minimum_configs < 3 or self.minimum_folds < 2:
            raise ValueError("minimum config/fold thresholds are too small")
        if not 0 < self.neighborhood_radius <= 1:
            raise ValueError("neighborhood_radius must be in (0,1]")
        if not 0 < self.reward_retention_ratio <= 1:
            raise ValueError("reward_retention_ratio must be in (0,1]")
        if self.minimum_neighbors <= 0:
            raise ValueError("minimum_neighbors must be positive")
        if not 0 <= self.minimum_plateau_neighbor_ratio <= 1:
            raise ValueError("minimum_plateau_neighbor_ratio must be in [0,1]")
        for name in ("maximum_false_action_rate", "maximum_wrong_action_rate"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")


@dataclass(frozen=True, slots=True)
class ConfigSummary:
    config_id: str
    parameters: tuple[tuple[str, float], ...]
    folds: int
    mean_reward: float
    minimum_fold_reward: float
    false_action_rate: float
    wrong_action_rate: float
    safety_eligible: bool


@dataclass(frozen=True, slots=True)
class NeighborEvidence:
    config_id: str
    normalized_distance: float
    mean_reward: float
    reward_retention: float
    plateau_member: bool


@dataclass(frozen=True, slots=True)
class HyperparameterPlateauReport:
    selected_config: str | None
    configs: tuple[ConfigSummary, ...]
    neighbors: tuple[NeighborEvidence, ...]
    selected_mean_reward: float
    selected_minimum_fold_reward: float
    plateau_neighbor_ratio: float
    minimum_neighbor_reward: float
    isolated_peak_gap: float
    status: PlateauStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is PlateauStatus.QUALIFIED


def _summaries(
    observations: Sequence[HyperparameterObservation],
    policy: HyperparameterPlateauPolicy,
) -> tuple[ConfigSummary, ...]:
    grouped: dict[str, list[HyperparameterObservation]] = defaultdict(list)
    for item in observations:
        grouped[item.config_id].append(item)
    output: list[ConfigSummary] = []
    for config_id in sorted(grouped):
        rows = sorted(grouped[config_id], key=lambda item: item.fold)
        parameter_keys = tuple(sorted(rows[0].parameters))
        parameter_values = tuple((key, rows[0].parameters[key]) for key in parameter_keys)
        if any(tuple(sorted(row.parameters)) != parameter_keys for row in rows):
            raise ValueError(f"config {config_id} has inconsistent parameter keys")
        if any(
            any(row.parameters[key] != value for key, value in parameter_values)
            for row in rows
        ):
            raise ValueError(f"config {config_id} changes parameters across folds")
        false_rate = statistics.fmean(row.false_action_rate for row in rows)
        wrong_rate = statistics.fmean(row.wrong_action_rate for row in rows)
        output.append(
            ConfigSummary(
                config_id=config_id,
                parameters=parameter_values,
                folds=len({row.fold for row in rows}),
                mean_reward=statistics.fmean(row.reward for row in rows),
                minimum_fold_reward=min(row.reward for row in rows),
                false_action_rate=false_rate,
                wrong_action_rate=wrong_rate,
                safety_eligible=(
                    false_rate <= policy.maximum_false_action_rate
                    and wrong_rate <= policy.maximum_wrong_action_rate
                ),
            )
        )
    return tuple(output)


def _ranges(configs: Sequence[ConfigSummary]) -> dict[str, tuple[float, float]]:
    keys = tuple(key for key, _ in configs[0].parameters) if configs else ()
    output: dict[str, tuple[float, float]] = {}
    for key in keys:
        values = [dict(config.parameters)[key] for config in configs]
        output[key] = (min(values), max(values))
    return output


def _distance(
    left: ConfigSummary,
    right: ConfigSummary,
    ranges: Mapping[str, tuple[float, float]],
) -> float:
    left_values = dict(left.parameters)
    right_values = dict(right.parameters)
    scaled: list[float] = []
    for key, (low, high) in ranges.items():
        width = high - low
        if width <= 0:
            continue
        scaled.append(abs(left_values[key] - right_values[key]) / width)
    return max(scaled, default=0.0)


def evaluate_hyperparameter_plateau(
    observations: Sequence[HyperparameterObservation],
    *,
    policy: HyperparameterPlateauPolicy | None = None,
) -> HyperparameterPlateauReport:
    """Reject tuned candidates whose held-out reward is an isolated parameter-space spike.

    Parameter distance is range-normalized L-infinity distance. The selected configuration is
    the highest mean-reward *safety-eligible* configuration. Nearby configurations must preserve
    a configurable fraction of that held-out reward. This is research-only and does not tune or
    deploy a model by itself.
    """
    policy = policy or HyperparameterPlateauPolicy()
    configs = _summaries(observations, policy)
    folds = {item.fold for item in observations}
    failures: list[str] = []
    if len(configs) < policy.minimum_configs:
        failures.append(f"insufficient_configs:{len(configs)}<{policy.minimum_configs}")
    if len(folds) < policy.minimum_folds:
        failures.append(f"insufficient_folds:{len(folds)}<{policy.minimum_folds}")
    eligible = [
        config
        for config in configs
        if config.safety_eligible and config.folds >= policy.minimum_folds
    ]
    if not eligible:
        failures.append("no_safety_eligible_config")
    if failures:
        return HyperparameterPlateauReport(
            selected_config=None,
            configs=configs,
            neighbors=(),
            selected_mean_reward=0.0,
            selected_minimum_fold_reward=0.0,
            plateau_neighbor_ratio=0.0,
            minimum_neighbor_reward=0.0,
            isolated_peak_gap=0.0,
            status=PlateauStatus.INSUFFICIENT,
            failures=tuple(failures),
        )

    selected = max(eligible, key=lambda item: (item.mean_reward, item.config_id))
    ranges = _ranges(configs)
    neighbors: list[NeighborEvidence] = []
    for config in configs:
        if config.config_id == selected.config_id or not config.safety_eligible:
            continue
        distance = _distance(selected, config, ranges)
        if distance > policy.neighborhood_radius:
            continue
        retention = (
            config.mean_reward / selected.mean_reward
            if selected.mean_reward > 0
            else (1.0 if config.mean_reward >= selected.mean_reward else 0.0)
        )
        neighbors.append(
            NeighborEvidence(
                config_id=config.config_id,
                normalized_distance=distance,
                mean_reward=config.mean_reward,
                reward_retention=retention,
                plateau_member=(
                    config.mean_reward > 0
                    and retention >= policy.reward_retention_ratio
                ),
            )
        )
    neighbors.sort(key=lambda item: (item.normalized_distance, item.config_id))
    plateau_ratio = (
        sum(item.plateau_member for item in neighbors) / len(neighbors)
        if neighbors
        else 0.0
    )
    minimum_neighbor = min((item.mean_reward for item in neighbors), default=0.0)
    best_neighbor = max((item.mean_reward for item in neighbors), default=0.0)
    isolated_gap = selected.mean_reward - best_neighbor
    if selected.mean_reward <= 0:
        failures.append("selected_config_mean_reward_not_positive")
    if len(neighbors) < policy.minimum_neighbors:
        failures.append(
            f"insufficient_parameter_neighbors:{len(neighbors)}<{policy.minimum_neighbors}"
        )
    if neighbors and plateau_ratio < policy.minimum_plateau_neighbor_ratio:
        failures.append(
            "plateau_neighbor_ratio_below_threshold:"
            f"{plateau_ratio:.6f}<{policy.minimum_plateau_neighbor_ratio:.6f}"
        )
    status = PlateauStatus.QUALIFIED if not failures else PlateauStatus.FAILED
    return HyperparameterPlateauReport(
        selected_config=selected.config_id,
        configs=configs,
        neighbors=tuple(neighbors),
        selected_mean_reward=selected.mean_reward,
        selected_minimum_fold_reward=selected.minimum_fold_reward,
        plateau_neighbor_ratio=plateau_ratio,
        minimum_neighbor_reward=minimum_neighbor,
        isolated_peak_gap=isolated_gap,
        status=status,
        failures=tuple(failures),
    )

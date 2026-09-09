from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .hyperparameter_plateau import HyperparameterObservation


class ParameterSurfaceStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class ParameterSurfacePolicy:
    neighborhood_radius: float = 0.35
    minimum_neighbors: int = 2
    maximum_relative_lipschitz: float = 2.5
    minimum_top_half_fold_ratio: float = 0.75
    maximum_neighbor_reward_cv: float = 0.35
    maximum_false_action_rate: float = 0.005
    maximum_wrong_action_rate: float = 0.01

    def __post_init__(self) -> None:
        if not 0 < self.neighborhood_radius <= 1:
            raise ValueError("neighborhood_radius must be in (0,1]")
        if self.minimum_neighbors <= 0:
            raise ValueError("minimum_neighbors must be positive")
        if self.maximum_relative_lipschitz < 0:
            raise ValueError("maximum_relative_lipschitz cannot be negative")
        if not 0 <= self.minimum_top_half_fold_ratio <= 1:
            raise ValueError("minimum_top_half_fold_ratio must be in [0,1]")
        if self.maximum_neighbor_reward_cv < 0:
            raise ValueError("maximum_neighbor_reward_cv cannot be negative")
        for name in ("maximum_false_action_rate", "maximum_wrong_action_rate"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")


@dataclass(frozen=True, slots=True)
class SurfaceNeighbor:
    config_id: str
    normalized_distance: float
    mean_reward: float
    relative_lipschitz: float
    dominant_axis: str


@dataclass(frozen=True, slots=True)
class AxisSensitivity:
    axis: str
    neighbors: int
    maximum_relative_lipschitz: float
    median_relative_lipschitz: float


@dataclass(frozen=True, slots=True)
class ParameterSurfaceReport:
    selected_config: str
    selected_mean_reward: float
    neighbors: tuple[SurfaceNeighbor, ...]
    axis_sensitivity: tuple[AxisSensitivity, ...]
    maximum_relative_lipschitz: float
    neighbor_reward_cv: float
    top_half_fold_ratio: float
    status: ParameterSurfaceStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is ParameterSurfaceStatus.QUALIFIED


@dataclass(frozen=True, slots=True)
class _Config:
    config_id: str
    parameters: tuple[tuple[str, float], ...]
    mean_reward: float
    folds: tuple[tuple[int, float], ...]
    false_action_rate: float
    wrong_action_rate: float


def _configs(observations: Sequence[HyperparameterObservation]) -> tuple[_Config, ...]:
    grouped: dict[str, list[HyperparameterObservation]] = defaultdict(list)
    for row in observations:
        grouped[row.config_id].append(row)
    output: list[_Config] = []
    for config_id in sorted(grouped):
        rows = sorted(grouped[config_id], key=lambda item: item.fold)
        parameters = tuple(sorted(rows[0].parameters.items()))
        if any(tuple(sorted(row.parameters.items())) != parameters for row in rows):
            raise ValueError(f"config {config_id} changes parameters across folds")
        output.append(
            _Config(
                config_id=config_id,
                parameters=parameters,
                mean_reward=statistics.fmean(row.reward for row in rows),
                folds=tuple((row.fold, row.reward) for row in rows),
                false_action_rate=statistics.fmean(row.false_action_rate for row in rows),
                wrong_action_rate=statistics.fmean(row.wrong_action_rate for row in rows),
            )
        )
    return tuple(output)


def _eligible(config: _Config, policy: ParameterSurfacePolicy) -> bool:
    return (
        config.false_action_rate <= policy.maximum_false_action_rate
        and config.wrong_action_rate <= policy.maximum_wrong_action_rate
    )


def _ranges(configs: Sequence[_Config]) -> dict[str, tuple[float, float]]:
    if not configs:
        return {}
    keys = tuple(key for key, _ in configs[0].parameters)
    ranges: dict[str, tuple[float, float]] = {}
    for key in keys:
        values = [dict(item.parameters)[key] for item in configs]
        ranges[key] = (min(values), max(values))
    return ranges


def _distance_and_axis(
    left: _Config,
    right: _Config,
    ranges: Mapping[str, tuple[float, float]],
) -> tuple[float, str]:
    left_values = dict(left.parameters)
    right_values = dict(right.parameters)
    changes: list[tuple[float, str]] = []
    for key, (low, high) in ranges.items():
        width = high - low
        change = abs(left_values[key] - right_values[key]) / width if width > 0 else 0.0
        changes.append((change, key))
    distance, axis = max(changes, default=(0.0, ""), key=lambda item: (item[0], item[1]))
    return distance, axis


def _top_half_fold_ratio(selected: _Config, eligible: Sequence[_Config]) -> float:
    selected_folds = dict(selected.folds)
    common = (
        sorted(
            set(selected_folds).intersection(
                *(
                    set(dict(item.folds))
                    for item in eligible
                    if item.config_id != selected.config_id
                )
            )
        )
        if len(eligible) > 1
        else sorted(selected_folds)
    )
    if not common:
        return 0.0
    hits = 0
    for fold in common:
        ranked = sorted(
            (
                (dict(item.folds)[fold], item.config_id)
                for item in eligible
                if fold in dict(item.folds)
            ),
            reverse=True,
        )
        selected_rank = next(
            index
            for index, (_, name) in enumerate(ranked)
            if name == selected.config_id
        )
        hits += int(selected_rank < max(1, (len(ranked) + 1) // 2))
    return hits / len(common)


def evaluate_parameter_surface(
    observations: Sequence[HyperparameterObservation],
    *,
    selected_config: str,
    policy: ParameterSurfacePolicy | None = None,
) -> ParameterSurfaceReport:
    """Measure local held-out sensitivity around a chosen hyperparameter configuration.

    The report is research-only. It rejects knife-edge surfaces where small normalized parameter
    perturbations imply disproportionate held-out reward changes, even if a discrete plateau test
    happens to find enough acceptable neighbors.
    """
    policy = policy or ParameterSurfacePolicy()
    configs = _configs(observations)
    by_id = {item.config_id: item for item in configs}
    if selected_config not in by_id:
        raise ValueError(f"unknown selected_config: {selected_config}")
    selected = by_id[selected_config]
    eligible = tuple(item for item in configs if _eligible(item, policy))
    failures: list[str] = []
    if not _eligible(selected, policy):
        failures.append("selected_config_not_safety_eligible")
    ranges = _ranges(configs)
    neighbors: list[SurfaceNeighbor] = []
    scale = max(abs(selected.mean_reward), 1e-9)
    for item in eligible:
        if item.config_id == selected.config_id:
            continue
        distance, axis = _distance_and_axis(selected, item, ranges)
        if distance <= 0 or distance > policy.neighborhood_radius:
            continue
        relative = abs(item.mean_reward - selected.mean_reward) / scale / distance
        neighbors.append(
            SurfaceNeighbor(
                config_id=item.config_id,
                normalized_distance=distance,
                mean_reward=item.mean_reward,
                relative_lipschitz=relative,
                dominant_axis=axis,
            )
        )
    neighbors.sort(key=lambda item: (item.normalized_distance, item.config_id))
    if len(neighbors) < policy.minimum_neighbors:
        failures.append(
            f"insufficient_surface_neighbors:{len(neighbors)}<{policy.minimum_neighbors}"
        )
    maximum = max((item.relative_lipschitz for item in neighbors), default=0.0)
    if maximum > policy.maximum_relative_lipschitz:
        failures.append(
            "local_surface_sensitivity_above_threshold:"
            f"{maximum:.6f}>{policy.maximum_relative_lipschitz:.6f}"
        )
    rewards = [item.mean_reward for item in neighbors]
    reward_mean = abs(statistics.fmean(rewards)) if rewards else 0.0
    reward_cv = (
        statistics.pstdev(rewards) / reward_mean
        if len(rewards) > 1 and reward_mean > 0
        else 0.0
    )
    if reward_cv > policy.maximum_neighbor_reward_cv:
        failures.append(
            f"neighbor_reward_cv_above_threshold:{reward_cv:.6f}>"
            f"{policy.maximum_neighbor_reward_cv:.6f}"
        )
    top_half = _top_half_fold_ratio(selected, eligible)
    if top_half < policy.minimum_top_half_fold_ratio:
        failures.append(
            f"selected_fold_rank_stability_below_threshold:{top_half:.6f}<"
            f"{policy.minimum_top_half_fold_ratio:.6f}"
        )
    grouped: dict[str, list[float]] = defaultdict(list)
    for item in neighbors:
        grouped[item.dominant_axis].append(item.relative_lipschitz)
    axis_reports = tuple(
        AxisSensitivity(
            axis=axis,
            neighbors=len(values),
            maximum_relative_lipschitz=max(values),
            median_relative_lipschitz=statistics.median(values),
        )
        for axis, values in sorted(grouped.items())
    )
    if any(item.startswith("insufficient_") for item in failures):
        status = ParameterSurfaceStatus.INSUFFICIENT
    elif failures:
        status = ParameterSurfaceStatus.FAILED
    else:
        status = ParameterSurfaceStatus.QUALIFIED
    return ParameterSurfaceReport(
        selected_config=selected.config_id,
        selected_mean_reward=selected.mean_reward,
        neighbors=tuple(neighbors),
        axis_sensitivity=axis_reports,
        maximum_relative_lipschitz=maximum,
        neighbor_reward_cv=reward_cv,
        top_half_fold_ratio=top_half,
        status=status,
        failures=tuple(failures),
    )

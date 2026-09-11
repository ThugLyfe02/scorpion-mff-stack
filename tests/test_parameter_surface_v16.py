from scorpion.hyperparameter_plateau import HyperparameterObservation
from scorpion.parameter_surface import (
    ParameterSurfacePolicy,
    ParameterSurfaceStatus,
    evaluate_parameter_surface,
)


def _surface(*, knife_edge: bool) -> tuple[HyperparameterObservation, ...]:
    configs = (
        ("a", 0.10, 0.20),
        ("b", 0.20, 0.25),
        ("c", 0.30, 0.30),
        ("d", 0.40, 0.35),
        ("e", 0.50, 0.40),
    )
    rows: list[HyperparameterObservation] = []
    for config_id, alpha, beta in configs:
        if knife_edge:
            base = 0.10 if config_id == "c" else 0.025
        else:
            base = {"a": 0.072, "b": 0.091, "c": 0.10, "d": 0.093, "e": 0.075}[config_id]
        for fold in range(4):
            fold_adjustment = (fold - 1.5) * 0.001
            rows.append(
                HyperparameterObservation(
                    config_id=config_id,
                    fold=fold,
                    parameters={"alpha": alpha, "beta": beta},
                    reward=base + fold_adjustment,
                )
            )
    return tuple(rows)


def test_parameter_surface_accepts_smooth_local_basin():
    report = evaluate_parameter_surface(
        _surface(knife_edge=False),
        selected_config="c",
        policy=ParameterSurfacePolicy(
            neighborhood_radius=0.35,
            minimum_neighbors=2,
            maximum_relative_lipschitz=1.0,
            minimum_top_half_fold_ratio=0.75,
            maximum_neighbor_reward_cv=0.10,
        ),
    )
    assert report.status is ParameterSurfaceStatus.QUALIFIED
    assert report.maximum_relative_lipschitz < 1.0
    assert report.top_half_fold_ratio == 1.0


def test_parameter_surface_rejects_knife_edge_optimum():
    report = evaluate_parameter_surface(
        _surface(knife_edge=True),
        selected_config="c",
        policy=ParameterSurfacePolicy(
            neighborhood_radius=0.35,
            minimum_neighbors=2,
            maximum_relative_lipschitz=1.5,
            minimum_top_half_fold_ratio=0.75,
        ),
    )
    assert report.status is ParameterSurfaceStatus.FAILED
    assert report.maximum_relative_lipschitz > 1.5
    assert any("local_surface_sensitivity" in item for item in report.failures)


def test_parameter_surface_excludes_unsafe_neighbor_from_shape_evidence():
    rows = list(_surface(knife_edge=False))
    for fold in range(4):
        rows.append(
            HyperparameterObservation(
                config_id="unsafe",
                fold=fold,
                parameters={"alpha": 0.31, "beta": 0.31},
                reward=0.50,
                false_action_rate=0.10,
            )
        )
    report = evaluate_parameter_surface(
        tuple(rows),
        selected_config="c",
        policy=ParameterSurfacePolicy(
            neighborhood_radius=0.35,
            minimum_neighbors=2,
            maximum_relative_lipschitz=1.0,
        ),
    )
    assert all(item.config_id != "unsafe" for item in report.neighbors)

from scorpion.hyperparameter_plateau import (
    HyperparameterObservation,
    HyperparameterPlateauPolicy,
    PlateauStatus,
    evaluate_hyperparameter_plateau,
)


def _observations(*, needle: bool) -> tuple[HyperparameterObservation, ...]:
    configs = (
        ("a", 0.10, 0.20),
        ("b", 0.20, 0.25),
        ("c", 0.30, 0.30),
        ("d", 0.40, 0.35),
        ("e", 0.50, 0.40),
    )
    rows: list[HyperparameterObservation] = []
    for config_id, alpha, beta in configs:
        if needle:
            base = 0.10 if config_id == "c" else 0.02
        else:
            base = {"a": 0.06, "b": 0.085, "c": 0.10, "d": 0.09, "e": 0.07}[config_id]
        for fold in range(4):
            rows.append(
                HyperparameterObservation(
                    config_id=config_id,
                    fold=fold,
                    parameters={"alpha": alpha, "beta": beta},
                    reward=base + fold * 0.001,
                )
            )
    return tuple(rows)


def test_hyperparameter_plateau_rejects_isolated_spike():
    report = evaluate_hyperparameter_plateau(
        _observations(needle=True),
        policy=HyperparameterPlateauPolicy(
            minimum_configs=5,
            minimum_folds=4,
            neighborhood_radius=0.35,
            reward_retention_ratio=0.80,
            minimum_neighbors=2,
            minimum_plateau_neighbor_ratio=0.50,
        ),
    )
    assert report.selected_config == "c"
    assert report.status is PlateauStatus.FAILED
    assert report.plateau_neighbor_ratio == 0.0
    assert report.isolated_peak_gap > 0.07


def test_hyperparameter_plateau_accepts_broad_stable_neighborhood():
    report = evaluate_hyperparameter_plateau(
        _observations(needle=False),
        policy=HyperparameterPlateauPolicy(
            minimum_configs=5,
            minimum_folds=4,
            neighborhood_radius=0.35,
            reward_retention_ratio=0.80,
            minimum_neighbors=2,
            minimum_plateau_neighbor_ratio=0.50,
        ),
    )
    assert report.selected_config == "c"
    assert report.status is PlateauStatus.QUALIFIED
    assert report.plateau_neighbor_ratio >= 0.50
    assert sum(item.plateau_member for item in report.neighbors) >= 2


def test_hyperparameter_plateau_excludes_unsafe_config_before_selection():
    rows = list(_observations(needle=False))
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
    report = evaluate_hyperparameter_plateau(
        tuple(rows),
        policy=HyperparameterPlateauPolicy(
            minimum_configs=5,
            minimum_folds=4,
            neighborhood_radius=0.35,
            minimum_neighbors=2,
            maximum_false_action_rate=0.005,
        ),
    )
    assert report.selected_config != "unsafe"
    unsafe = next(item for item in report.configs if item.config_id == "unsafe")
    assert unsafe.safety_eligible is False

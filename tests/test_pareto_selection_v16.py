from scorpion.pareto_selection import (
    MultiObjectiveCandidate,
    ParetoSelectionPolicy,
    ParetoStatus,
    evaluate_pareto_selection,
)


def test_pareto_selection_removes_dominated_candidate_and_preserves_tradeoffs():
    report = evaluate_pareto_selection(
        (
            MultiObjectiveCandidate("accurate", 0.985, 0.030, 8.0, 0.92, 4.0),
            MultiObjectiveCandidate("fast", 0.975, 0.025, 3.0, 0.91, 2.0),
            MultiObjectiveCandidate("dominated", 0.970, 0.040, 10.0, 0.85, 5.0),
        ),
        policy=ParetoSelectionPolicy(minimum_accuracy=0.95),
    )
    assert report.status is ParetoStatus.QUALIFIED
    assert set(report.frontier) == {"accurate", "fast"}
    dominated = next(item for item in report.evidence if item.candidate_id == "dominated")
    assert dominated.pareto_optimal is False
    assert dominated.dominated_by


def test_pareto_selection_excludes_unsafe_high_metric_candidate_before_frontier():
    report = evaluate_pareto_selection(
        (
            MultiObjectiveCandidate("safe-a", 0.980, 0.030, 6.0, 0.90, 3.0),
            MultiObjectiveCandidate("safe-b", 0.978, 0.025, 4.0, 0.91, 2.5),
            MultiObjectiveCandidate(
                "unsafe",
                0.999,
                0.005,
                1.0,
                0.99,
                1.0,
                false_action_rate=0.05,
            ),
        ),
        policy=ParetoSelectionPolicy(maximum_false_action_rate=0.005),
    )
    assert report.status is ParetoStatus.QUALIFIED
    assert "unsafe" not in report.frontier
    unsafe = next(item for item in report.evidence if item.candidate_id == "unsafe")
    assert unsafe.safety_eligible is False
    assert "false_action_rate_above_ceiling" in unsafe.safety_failures


def test_pareto_balanced_champion_is_on_frontier():
    report = evaluate_pareto_selection(
        (
            MultiObjectiveCandidate("a", 0.990, 0.040, 10.0, 0.95, 4.0),
            MultiObjectiveCandidate("b", 0.982, 0.020, 5.0, 0.93, 2.0),
            MultiObjectiveCandidate("c", 0.975, 0.015, 3.0, 0.90, 1.0),
        )
    )
    assert report.status is ParetoStatus.QUALIFIED
    assert report.balanced_champion in report.frontier

from scorpion.health import DecisionHealthRule, evaluate_decision_health
from scorpion.performance import LatencyBudget, evaluate_latency


def test_decision_health_detects_quality_degradation():
    metrics = {
        "count": 100,
        "ambiguity_rate": 0.50,
        "low_confidence_rate": 0.05,
        "unresolved_association_rate": 0.05,
        "parser_p95_us": 500.0,
        "pipeline_p95_us": 5_000.0,
    }
    status = evaluate_decision_health(metrics, DecisionHealthRule())
    assert status.ok is False
    assert any(item.startswith("ambiguity_rate") for item in status.failures)


def test_latency_budget_reports_p95():
    report = evaluate_latency(
        [100, 200, 300, 400, 500],
        [1_000, 2_000, 3_000, 4_000, 5_000],
        LatencyBudget(parser_p95_us=600, pipeline_p95_us=6_000),
    )
    assert report.within_budget is True
    assert report.parser_p95_us == 500.0
    assert report.pipeline_p95_us == 5_000.0

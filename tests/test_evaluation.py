from dataclasses import replace

from scorpion.accuracy import AccuracyReport
from scorpion.domain import EventKind
from scorpion.evaluation import (
    PromotionCriteria,
    compare_parser_versions,
    evaluate_promotion,
)
from scorpion.parser import parse_message


def test_version_diff_flags_action_escalation(raw_factory):
    messages = [raw_factory("Nice bounce +10%", message_id="x")]

    def candidate(raw):
        return replace(parse_message(raw), kind=EventKind.EXIT)

    comparison = compare_parser_versions(messages, parse_message, candidate)
    assert comparison.changed == 1
    assert comparison.action_escalations == 1


def test_promotion_gate_rejects_low_sample_candidate():
    report = AccuracyReport(
        total=10,
        correct=10,
        accuracy=1.0,
        actionable_precision=1.0,
        ambiguity_rate=0.0,
        per_kind={},
    )
    comparison = compare_parser_versions([], parse_message, parse_message)
    decision = evaluate_promotion(
        report,
        {
            "ambiguity_rate": 0.0,
            "parser_p95_us": 100.0,
            "pipeline_p95_us": 1_000.0,
        },
        comparison,
        PromotionCriteria(min_adjudicated_samples=100),
    )
    assert decision.promotable is False
    assert any(item.startswith("samples:") for item in decision.failures)

import json
from pathlib import Path

from scorpion.accuracy import LabeledDecision, compare_distributions, score_decisions
from scorpion.domain import EventKind
from scorpion.parser import parse_message_with_evidence


def test_golden_corpus_is_exact(raw_factory):
    path = Path(__file__).parent / "fixtures" / "mff_golden.json"
    rows = json.loads(path.read_text())
    samples: list[LabeledDecision] = []
    for index, row in enumerate(rows):
        decision = parse_message_with_evidence(
            raw_factory(row["text"], message_id=str(index + 1))
        )
        expected = EventKind(row["kind"])
        samples.append(LabeledDecision(expected=expected, predicted=decision.event.kind))
        assert decision.event.kind is expected, row["text"]
        assert 0.0 <= decision.evidence.confidence <= 1.0
        assert decision.evidence.rule_id
    report = score_decisions(samples)
    assert report.accuracy == 1.0
    assert report.actionable_precision == 1.0


def test_distribution_drift_is_detected():
    baseline = [EventKind.ENTRY] * 8 + [EventKind.EXIT] * 2
    current = [EventKind.AMBIGUOUS] * 8 + [EventKind.EXIT] * 2
    report = compare_distributions(baseline, current, threshold=0.3)
    assert report.drifted is True
    assert report.total_variation >= 0.8

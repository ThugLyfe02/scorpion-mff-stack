from scorpion.domain import EventKind
from scorpion.parser import parse_message_with_evidence
from scorpion.tournament import TournamentCandidate, run_parser_tournament


def test_tournament_scores_accuracy_robustness_and_downstream_state(raw_factory):
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    report = run_parser_tournament(
        [raw],
        [
            TournamentCandidate("baseline", parse_message_with_evidence),
            TournamentCandidate("candidate", parse_message_with_evidence),
        ],
        expected_kinds={raw.revision_id: EventKind.ENTRY},
    )
    assert report.baseline_name == "baseline"
    assert len(report.scores) == 2
    baseline, candidate = report.scores
    assert baseline.accuracy == 1.0
    assert baseline.actionable_precision == 1.0
    assert baseline.grammar_action_leaks == 0
    assert candidate.downstream_delta is not None
    assert candidate.downstream_delta.fingerprint_changed is False
    assert candidate.action_escalations_vs_baseline == 0

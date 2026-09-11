from scorpion.domain import EventKind
from scorpion.parser import parse_message_with_evidence
from scorpion.selective import SelectiveObservation, choose_selective_policy
from scorpion.tournament import (
    TournamentCandidate,
    TournamentCriteria,
    run_parser_tournament,
)


def test_selective_search_uses_simultaneous_bound_across_thresholds():
    observations = tuple(
        SelectiveObservation(1.0 - index / 1000.0, True, True)
        for index in range(100)
    )
    policy = choose_selective_policy(
        observations,
        max_error_upper_95=0.05,
        min_coverage=0.50,
        min_samples=50,
    )
    assert policy.thresholds_tested == 100
    assert policy.enabled is False
    assert "selection_adjusted" in policy.reason


def test_selective_independent_holdout_can_certify_selected_threshold():
    discovery = tuple(
        SelectiveObservation(1.0 - index / 1000.0, True, True)
        for index in range(100)
    )
    validation = tuple(SelectiveObservation(0.99, True, True) for _ in range(200))
    policy = choose_selective_policy(
        discovery,
        validation_observations=validation,
        max_error_upper_95=0.05,
        min_coverage=0.50,
        min_samples=50,
    )
    assert policy.enabled is True
    assert policy.validation_samples == 200
    assert policy.reason == "independent_holdout_risk_bound_satisfied"
    assert policy.error_upper_95 < 0.05


def test_selective_independent_holdout_rejects_discovery_overfit():
    discovery = tuple(SelectiveObservation(0.99, True, True) for _ in range(100))
    validation = tuple(
        SelectiveObservation(0.99, index % 10 != 0, True) for index in range(100)
    )
    policy = choose_selective_policy(
        discovery,
        validation_observations=validation,
        max_error_upper_95=0.05,
        min_coverage=0.50,
        min_samples=50,
    )
    assert policy.enabled is False
    assert policy.reason == "independent_validation_did_not_support_target_risk"


def _permissive_tournament_criteria(**overrides):
    values = {
        "min_labeled_samples": 1,
        "min_accuracy": 0.90,
        "min_actionable_precision": 0.90,
        "min_label_coverage": 1.0,
        "min_actionable_contract_coverage": 1.0,
        "min_distinct_labeled_kinds": 1,
        "max_parser_p95_us": 1_000_000.0,
        "max_total_p95_us": 1_000_000.0,
    }
    values.update(overrides)
    return TournamentCriteria(**values)


def test_tournament_rejects_actionable_kind_truth_without_contract_truth(raw_factory):
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    report = run_parser_tournament(
        (raw,),
        (TournamentCandidate("baseline", parse_message_with_evidence),),
        expected_kinds={raw.revision_id: EventKind.ENTRY},
        criteria=_permissive_tournament_criteria(),
    )
    score = report.scores[0]
    assert score.actionable_labeled_samples == 1
    assert score.actionable_contract_labels == 0
    assert score.actionable_contract_coverage == 0.0
    assert score.missing_actionable_contract_labels == 1
    assert score.qualified is False
    assert any("missing_actionable_contract_labels" in item for item in score.failures)


def test_tournament_qualifies_only_after_actionable_contract_truth_is_supplied(raw_factory):
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    parsed = parse_message_with_evidence(raw, None).event
    assert parsed.contract_key is not None
    report = run_parser_tournament(
        (raw,),
        (TournamentCandidate("baseline", parse_message_with_evidence),),
        expected_kinds={raw.revision_id: EventKind.ENTRY},
        expected_contracts={raw.revision_id: parsed.contract_key},
        criteria=_permissive_tournament_criteria(),
    )
    score = report.scores[0]
    assert score.actionable_contract_coverage == 1.0
    assert score.contract_label_errors == 0
    assert score.missing_actionable_contract_labels == 0
    assert score.qualified is True


def test_tournament_rejects_sparse_kind_label_coverage(raw_factory):
    first = raw_factory("QQQ 719C TODAY @ 1.01", message_id="coverage-1")
    second = raw_factory("closing runners", message_id="coverage-2", minute=1)
    parsed = parse_message_with_evidence(first, None).event
    assert parsed.contract_key is not None
    report = run_parser_tournament(
        (first, second),
        (TournamentCandidate("baseline", parse_message_with_evidence),),
        expected_kinds={first.revision_id: EventKind.ENTRY},
        expected_contracts={first.revision_id: parsed.contract_key},
        criteria=_permissive_tournament_criteria(min_label_coverage=0.80),
    )
    score = report.scores[0]
    assert score.label_coverage == 0.5
    assert score.qualified is False
    assert any(item.startswith("label_coverage:") for item in score.failures)


def test_tournament_can_require_label_class_breadth(raw_factory):
    rows = tuple(
        raw_factory(
            f"QQQ {719 + index}C TODAY @ 1.01",
            message_id=f"breadth-{index}",
            minute=index,
        )
        for index in range(3)
    )
    expected_kinds = {row.revision_id: EventKind.ENTRY for row in rows}
    expected_contracts = {
        row.revision_id: parse_message_with_evidence(row, None).event.contract_key
        for row in rows
    }
    report = run_parser_tournament(
        rows,
        (TournamentCandidate("baseline", parse_message_with_evidence),),
        expected_kinds=expected_kinds,
        expected_contracts=expected_contracts,
        criteria=_permissive_tournament_criteria(min_distinct_labeled_kinds=2),
    )
    score = report.scores[0]
    assert score.distinct_labeled_kinds == 1
    assert score.qualified is False
    assert any(item.startswith("distinct_labeled_kinds:") for item in score.failures)

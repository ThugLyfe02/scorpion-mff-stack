from scorpion.canary import (
    CanaryPolicy,
    CanaryStatus,
    PairedCanaryObservation,
    evaluate_canary,
)
from scorpion.counterfactual import CounterfactualDelta
from scorpion.governance import PromotionEvidence, PromotionStatus, evaluate_promotion
from scorpion.selective import SelectivePolicy
from scorpion.tournament import CandidateScore
from scorpion.walk_forward import WalkForwardReport


def _candidate() -> CandidateScore:
    return CandidateScore(
        name="candidate",
        labeled_samples=200,
        accuracy=0.99,
        actionable_precision=1.0,
        ambiguity_rate=0.01,
        parser_p95_us=500.0,
        total_p95_us=1500.0,
        surface_kind_changes=0,
        surface_contract_changes=0,
        grammar_action_leaks=0,
        contract_label_errors=0,
        review_effects=2,
        action_escalations_vs_baseline=0,
        downstream_delta=CounterfactualDelta(False, 0, 0, 0),
        fingerprint="a" * 64,
        qualified=True,
        failures=(),
    )


def test_paired_canary_quarantines_single_dangerous_action_escalation():
    observations = tuple(
        PairedCanaryObservation(
            champion_correct=True,
            challenger_correct=True,
            champion_actionable=False,
            challenger_actionable=(index == 9),
            contract_diverged=False,
            challenger_latency_us=110,
            champion_latency_us=100,
        )
        for index in range(10)
    )
    report = evaluate_canary(
        observations,
        policy=CanaryPolicy(minimum_pairs=10, maximum_action_escalations=0),
    )
    assert report.status is CanaryStatus.QUARANTINE
    assert report.action_escalations == 1


def test_paired_canary_can_reach_operator_review_without_self_promotion():
    observations = tuple(
        PairedCanaryObservation(
            champion_correct=index % 20 != 0,
            challenger_correct=True,
            champion_actionable=True,
            challenger_actionable=True,
            contract_diverged=False,
            challenger_latency_us=105,
            champion_latency_us=100,
        )
        for index in range(120)
    )
    canary = evaluate_canary(
        observations,
        policy=CanaryPolicy(minimum_pairs=100, minimum_accuracy_delta=-0.01),
    )
    assert canary.status is CanaryStatus.READY_FOR_OPERATOR_REVIEW

    evidence = PromotionEvidence(
        candidate=_candidate(),
        selective_policy=SelectivePolicy(True, 0.99, 100, 0.5, 0.03, "supported"),
        walk_forward=WalkForwardReport((), 100, 0.05, 1.0, 0.02, True, ()),
        research_manifest_hash="manifest",
        dataset_complete=True,
        quote_coverage_ok=True,
        depth_coverage_ok=True,
        canary=canary,
        runtime_certified=True,
    )
    promotion = evaluate_promotion(evidence)
    assert promotion.status is PromotionStatus.READY_FOR_OPERATOR_REVIEW
    assert "operator review" in promotion.reason

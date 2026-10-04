from scorpion.domain import EventKind
from scorpion.hard_example_curriculum import (
    CurriculumExample,
    HardExampleCurriculumPolicy,
    build_hard_example_curriculum,
)
from scorpion.information_value import (
    InformationValuePolicy,
    LearningAction,
    LearningSignal,
    LearningValueAssessment,
    allocate_learning_budget,
    assess_information_value,
)
from scorpion.uncertainty_decomposition import (
    UncertaintyDecomposition,
    UncertaintyStatus,
)


def _uncertainty(
    *,
    epistemic: float,
    aleatoric: float,
    variation: float,
    status: UncertaintyStatus,
) -> UncertaintyDecomposition:
    return UncertaintyDecomposition(
        predictive_entropy=0.5,
        expected_model_entropy=0.25,
        mutual_information=0.25,
        normalized_epistemic=epistemic,
        normalized_aleatoric=aleatoric,
        variation_ratio=variation,
        top_label=EventKind.ENTRY,
        top_probability=0.6,
        status=status,
    )


def test_information_value_prefers_learnable_uncertainty_over_intrinsic_ambiguity():
    learnable = assess_information_value(
        LearningSignal(
            event_id="learnable",
            uncertainty=_uncertainty(
                epistemic=0.75,
                aleatoric=0.10,
                variation=0.50,
                status=UncertaintyStatus.CONFLICTED,
            ),
            residual_hotspot_priority=1.5,
            novelty=0.7,
        )
    )
    ambiguous = assess_information_value(
        LearningSignal(
            event_id="ambiguous",
            uncertainty=_uncertainty(
                epistemic=0.05,
                aleatoric=0.90,
                variation=0.10,
                status=UncertaintyStatus.INHERENTLY_AMBIGUOUS,
            ),
            residual_hotspot_priority=1.5,
            novelty=0.7,
        )
    )
    assert learnable.expected_information_value > ambiguous.expected_information_value
    assert learnable.deep_utility > ambiguous.deep_utility
    assert ambiguous.intrinsically_ambiguous
    assert "intrinsic_ambiguity_discount" in ambiguous.reasons


def test_hotspot_scarcity_and_coverage_raise_expected_information_value():
    uncertainty = _uncertainty(
        epistemic=0.45,
        aleatoric=0.15,
        variation=0.35,
        status=UncertaintyStatus.CONFLICTED,
    )
    baseline = assess_information_value(LearningSignal(event_id="base", uncertainty=uncertainty))
    strategic = assess_information_value(
        LearningSignal(
            event_id="strategic",
            uncertainty=uncertainty,
            residual_hotspot_priority=2.0,
            label_scarcity=0.9,
            coverage_deficit=0.8,
            actionable_disagreement=True,
        )
    )
    assert strategic.expected_information_value > baseline.expected_information_value
    assert strategic.review_utility > baseline.review_utility
    assert "robust_residual_hotspot" in strategic.reasons
    assert "coverage_deficit" in strategic.reasons


def test_learning_budget_is_exact_deterministic_and_never_exceeds_budget():
    policy = InformationValuePolicy(
        minimum_light_utility=0.0,
        minimum_deep_utility=0.0,
        minimum_review_utility=0.0,
    )
    assessments = (
        LearningValueAssessment("a", 0.5, 0.5, 1.0, 0.60, 0.80, 0.90, False, ()),
        LearningValueAssessment("b", 0.5, 0.5, 1.0, 0.55, 0.95, 0.70, False, ()),
        LearningValueAssessment("c", 0.5, 0.5, 1.0, 0.40, 0.50, 0.65, False, ()),
    )
    first = allocate_learning_budget(assessments, budget_units=6, policy=policy)
    second = allocate_learning_budget(tuple(reversed(assessments)), budget_units=6, policy=policy)
    assert first.used_units <= 6
    assert len({item.event_id for item in first.decisions}) == len(first.decisions)
    assert first.allocation_hash == second.allocation_hash
    assert first.total_utility == second.total_utility


def test_intrinsically_ambiguous_case_consumes_no_learning_budget():
    policy = InformationValuePolicy(
        minimum_light_utility=0.0,
        minimum_deep_utility=0.0,
        minimum_review_utility=0.0,
    )
    assessment = assess_information_value(
        LearningSignal(
            event_id="ambiguous",
            uncertainty=_uncertainty(
                epistemic=0.01,
                aleatoric=0.95,
                variation=0.0,
                status=UncertaintyStatus.INHERENTLY_AMBIGUOUS,
            ),
        ),
        policy=policy,
    )
    allocation = allocate_learning_budget((assessment,), budget_units=20, policy=policy)
    assert allocation.decisions == ()
    assert allocation.used_units == 0


def _example(
    event_id: str,
    label: EventKind,
    *,
    confidence: float = 0.98,
    information: float = 0.7,
    hotspot: float = 1.0,
    epistemic: float = 0.5,
    aleatoric: float = 0.1,
    channel: str = "mff",
) -> CurriculumExample:
    return CurriculumExample(
        event_id=event_id,
        label=label,
        label_source_id=f"consensus:{event_id}",
        label_confidence=confidence,
        information_value=information,
        residual_hotspot_score=hotspot,
        epistemic=epistemic,
        aleatoric=aleatoric,
        slices={"channel": channel, "rule": f"rule-{event_id}"},
    )


def test_curriculum_never_leaks_frozen_holdout_or_low_quality_labels():
    examples = (
        _example("train-good", EventKind.ENTRY),
        _example("holdout", EventKind.ENTRY),
        _example("low-confidence", EventKind.IGNORE, confidence=0.5),
        _example("too-ambiguous", EventKind.IGNORE, aleatoric=0.9),
    )
    curriculum = build_hard_example_curriculum(
        examples,
        source_dataset_fingerprint="dataset-v1",
        holdout_event_ids=frozenset({"holdout"}),
    )
    selected = {item.event_id for item in curriculum.selected}
    assert selected == {"train-good"}
    exclusions = dict(curriculum.exclusions)
    assert exclusions["holdout"] == "frozen_holdout"
    assert exclusions["low-confidence"] == "insufficient_label_confidence"
    assert exclusions["too-ambiguous"] == "excessive_aleatoric_uncertainty"


def test_curriculum_preserves_label_breadth_and_limits_dominant_slice():
    policy = HardExampleCurriculumPolicy(
        maximum_examples=4,
        maximum_label_fraction=0.75,
        maximum_slice_fraction=0.75,
        minimum_examples_per_label=1,
    )
    examples = (
        _example("entry-1", EventKind.ENTRY, information=0.95, channel="dominant"),
        _example("entry-2", EventKind.ENTRY, information=0.94, channel="dominant"),
        _example("entry-3", EventKind.ENTRY, information=0.93, channel="dominant"),
        _example("entry-4", EventKind.ENTRY, information=0.92, channel="dominant"),
        _example("ignore-1", EventKind.IGNORE, information=0.60, channel="other"),
    )
    curriculum = build_hard_example_curriculum(
        examples,
        source_dataset_fingerprint="dataset-v1",
        policy=policy,
    )
    labels = {item.label for item in curriculum.selected}
    assert EventKind.ENTRY in labels
    assert EventKind.IGNORE in labels
    assert dict(curriculum.slice_counts).get("channel=dominant", 0) <= 3


def test_curriculum_fingerprint_is_order_invariant_and_binds_dataset_identity():
    examples = (
        _example("a", EventKind.ENTRY, information=0.8),
        _example("b", EventKind.IGNORE, information=0.7, channel="other"),
        _example("c", EventKind.ADD, information=0.6, channel="other-2"),
    )
    first = build_hard_example_curriculum(
        examples,
        source_dataset_fingerprint="dataset-v1",
    )
    reordered = build_hard_example_curriculum(
        tuple(reversed(examples)),
        source_dataset_fingerprint="dataset-v1",
    )
    other_dataset = build_hard_example_curriculum(
        examples,
        source_dataset_fingerprint="dataset-v2",
    )
    assert first.curriculum_fingerprint == reordered.curriculum_fingerprint
    assert first.curriculum_fingerprint != other_dataset.curriculum_fingerprint


def test_learning_allocator_actions_are_research_only_types():
    assessment = LearningValueAssessment(
        "x",
        0.8,
        0.8,
        1.0,
        0.7,
        0.9,
        1.0,
        False,
        ("actionable_disagreement",),
    )
    allocation = allocate_learning_budget((assessment,), budget_units=8)
    assert all(
        decision.action
        in {LearningAction.LIGHT_SHADOW, LearningAction.DEEP_SHADOW, LearningAction.HUMAN_REVIEW}
        for decision in allocation.decisions
    )

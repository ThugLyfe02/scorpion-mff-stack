from scorpion.evidence_lattice import (
    ResearchAuthorityTier,
    ResearchEvidence,
    evaluate_research_authority,
)
from scorpion.liquidity_capacity import LiquidityCapacityReport


def _base(**overrides: object) -> ResearchEvidence:
    values: dict[str, object] = {
        "code_provenance_ok": True,
        "history_complete": True,
        "market_data_quality_ok": True,
        "quote_coverage_ok": True,
        "contract_terms_ok": True,
        "tick_alignment_ok": True,
        "fill_model_trust_ok": True,
        "statistical_selection_ok": True,
        "walk_forward_ok": True,
    }
    values.update(overrides)
    return ResearchEvidence(**values)  # type: ignore[arg-type]


def test_failed_core_evidence_never_exceeds_diagnostic_authority():
    decision = evaluate_research_authority(_base(history_complete=False))
    assert decision.tier is ResearchAuthorityTier.DIAGNOSTIC_ONLY
    assert "history_not_complete" in decision.failures
    assert decision.research_ready is False


def test_research_can_be_ready_without_granting_sizing_research_authority():
    decision = evaluate_research_authority(_base())
    assert decision.tier is ResearchAuthorityTier.RESEARCH_READY
    assert decision.research_ready is True
    assert decision.sizing_research_ready is False
    assert decision.sizing_failures == ("liquidity_capacity_not_supplied",)


def test_sizing_research_requires_robust_capacity_evidence():
    capacity = LiquidityCapacityReport(
        scenarios=(),
        frontier=(),
        max_robust_clip_multiplier=1.0,
        robust=True,
    )
    decision = evaluate_research_authority(_base(liquidity_capacity=capacity))
    assert decision.tier is ResearchAuthorityTier.SIZING_RESEARCH_READY
    assert decision.sizing_research_ready is True


def test_weak_capacity_does_not_destroy_research_but_blocks_sizing():
    capacity = LiquidityCapacityReport(
        scenarios=(),
        frontier=(),
        max_robust_clip_multiplier=0.0,
        robust=False,
    )
    decision = evaluate_research_authority(_base(liquidity_capacity=capacity))
    assert decision.tier is ResearchAuthorityTier.RESEARCH_READY
    assert any(item.startswith("liquidity_capacity_failed:") for item in decision.sizing_failures)

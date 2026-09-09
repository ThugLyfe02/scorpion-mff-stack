from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .backtest_overfit import BacktestOverfitReport
from .distribution_robust import DistributionRobustReport
from .feed_integrity import FeedIntegrityReport
from .liquidity_capacity import LiquidityCapacityReport
from .regime_stability import RegimeStabilityReport


class ResearchAuthorityTier(StrEnum):
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
    RESEARCH_READY = "RESEARCH_READY"
    SIZING_RESEARCH_READY = "SIZING_RESEARCH_READY"


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    code_provenance_ok: bool
    history_complete: bool
    market_data_quality_ok: bool
    quote_coverage_ok: bool
    contract_terms_ok: bool
    tick_alignment_ok: bool
    fill_model_trust_ok: bool
    statistical_selection_ok: bool
    walk_forward_ok: bool
    regime_stability: RegimeStabilityReport | None = None
    distribution_robustness: DistributionRobustReport | None = None
    backtest_overfit: BacktestOverfitReport | None = None
    liquidity_capacity: LiquidityCapacityReport | None = None
    feed_integrity: FeedIntegrityReport | None = None


@dataclass(frozen=True, slots=True)
class ResearchAuthorityDecision:
    tier: ResearchAuthorityTier
    failures: tuple[str, ...]
    sizing_failures: tuple[str, ...]

    @property
    def research_ready(self) -> bool:
        return self.tier in {
            ResearchAuthorityTier.RESEARCH_READY,
            ResearchAuthorityTier.SIZING_RESEARCH_READY,
        }

    @property
    def sizing_research_ready(self) -> bool:
        return self.tier is ResearchAuthorityTier.SIZING_RESEARCH_READY


def evaluate_research_authority(evidence: ResearchEvidence) -> ResearchAuthorityDecision:
    """Evaluate one canonical evidence lattice for all research entry points.

    The function deliberately distinguishes *diagnostic access* from *research conclusions* and
    *sizing research*. A missing proof never prevents forensic inspection; it only prevents a
    stronger authority tier. This makes fail-closed behavior composable without hiding data from
    operators and prevents separate CLIs from inventing subtly different promotion rules.
    """

    failures: list[str] = []
    for ok, reason in (
        (evidence.code_provenance_ok, "code_provenance_missing"),
        (evidence.history_complete, "history_not_complete"),
        (evidence.market_data_quality_ok, "market_data_quality_failed"),
        (evidence.quote_coverage_ok, "quote_coverage_insufficient"),
        (evidence.contract_terms_ok, "contract_terms_not_verified"),
        (evidence.tick_alignment_ok, "limit_tick_alignment_failed"),
        (evidence.fill_model_trust_ok, "fill_model_not_trusted"),
        (evidence.statistical_selection_ok, "statistical_selection_failed"),
        (evidence.walk_forward_ok, "walk_forward_failed"),
    ):
        if not ok:
            failures.append(reason)

    if evidence.feed_integrity is not None and not evidence.feed_integrity.certified:
        failures.append("feed_integrity_not_certified")
        failures.extend(f"feed:{item}" for item in evidence.feed_integrity.failures)
    if evidence.regime_stability is not None and not evidence.regime_stability.passed:
        failures.append("regime_stability_failed")
        failures.extend(f"regime:{item}" for item in evidence.regime_stability.failures)
    if (
        evidence.distribution_robustness is not None
        and not evidence.distribution_robustness.passed
    ):
        failures.append("distribution_shift_robustness_failed")
        failures.extend(
            f"distribution:{item}" for item in evidence.distribution_robustness.failures
        )
    if evidence.backtest_overfit is not None and not evidence.backtest_overfit.passed:
        failures.append("backtest_selection_overfit_failed")
        failures.extend(f"pbo:{item}" for item in evidence.backtest_overfit.failures)

    if failures:
        return ResearchAuthorityDecision(
            ResearchAuthorityTier.DIAGNOSTIC_ONLY,
            tuple(failures),
            (),
        )

    sizing_failures: list[str] = []
    if evidence.liquidity_capacity is None:
        sizing_failures.append("liquidity_capacity_not_supplied")
    elif not evidence.liquidity_capacity.robust:
        sizing_failures.append(
            "liquidity_capacity_failed:"
            f"max_robust_clip_multiplier={evidence.liquidity_capacity.max_robust_clip_multiplier:.3f}"
        )

    if sizing_failures:
        return ResearchAuthorityDecision(
            ResearchAuthorityTier.RESEARCH_READY,
            (),
            tuple(sizing_failures),
        )
    return ResearchAuthorityDecision(
        ResearchAuthorityTier.SIZING_RESEARCH_READY,
        (),
        (),
    )

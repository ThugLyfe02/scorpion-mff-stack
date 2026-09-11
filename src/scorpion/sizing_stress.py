from __future__ import annotations

from dataclasses import dataclass, replace

from .liquidity_capacity import LiquidityCapacityReport
from .live_sizing_safety import (
    LiveRiskState,
    LiveSizingDecision,
    LiveSizingRequest,
    LiveSizingSafetyPolicy,
    LiveSizingStatus,
    evaluate_live_sizing_request,
)


@dataclass(frozen=True, slots=True)
class SizingStressScenario:
    name: str
    daily_loss_add: float = 0.0
    drawdown_add: float = 0.0
    gross_exposure_add: float = 0.0
    cluster_exposure_add: float = 0.0
    quote_age_multiplier: float = 1.0
    decision_latency_multiplier: float = 1.0
    capacity_multiplier: float = 1.0
    force_quote_consensus_bad: bool = False
    force_runtime_uncertified: bool = False
    force_canary_unhealthy: bool = False
    force_drift_active: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("scenario name is required")
        multipliers = (
            "quote_age_multiplier",
            "decision_latency_multiplier",
            "capacity_multiplier",
        )
        for name in multipliers:
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class SizingStressPoint:
    scenario: str
    decision: LiveSizingDecision
    stressed_hard_ceiling_fraction: float
    became_more_permissive: bool


@dataclass(frozen=True, slots=True)
class SizingStressReport:
    baseline: LiveSizingDecision
    points: tuple[SizingStressPoint, ...]
    blocked_scenarios: int
    unsafe_permits: tuple[str, ...]
    monotonicity_violations: tuple[str, ...]
    passed: bool


def default_sizing_stress_scenarios() -> tuple[SizingStressScenario, ...]:
    return (
        SizingStressScenario("quote_age_2x", quote_age_multiplier=2.0),
        SizingStressScenario("latency_2x", decision_latency_multiplier=2.0),
        SizingStressScenario("capacity_half", capacity_multiplier=0.5),
        SizingStressScenario("daily_loss_shock", daily_loss_add=0.02),
        SizingStressScenario("drawdown_shock", drawdown_add=0.05),
        SizingStressScenario("gross_exposure_shock", gross_exposure_add=0.25),
        SizingStressScenario("cluster_exposure_shock", cluster_exposure_add=0.12),
        SizingStressScenario("quote_consensus_loss", force_quote_consensus_bad=True),
        SizingStressScenario("runtime_certification_loss", force_runtime_uncertified=True),
        SizingStressScenario("canary_degradation", force_canary_unhealthy=True),
        SizingStressScenario("drift_reappearance", force_drift_active=True),
        SizingStressScenario(
            "compound_microstructure_stress",
            quote_age_multiplier=4.0,
            decision_latency_multiplier=3.0,
            capacity_multiplier=0.5,
            force_quote_consensus_bad=True,
        ),
        SizingStressScenario(
            "compound_portfolio_stress",
            daily_loss_add=0.03,
            drawdown_add=0.10,
            gross_exposure_add=0.50,
            cluster_exposure_add=0.20,
        ),
    )


def _stressed_capacity(
    capacity: LiquidityCapacityReport,
    multiplier: float,
) -> LiquidityCapacityReport:
    new_max = max(0.0, capacity.max_robust_clip_multiplier * multiplier)
    return replace(
        capacity,
        max_robust_clip_multiplier=new_max,
        robust=capacity.robust and new_max > 0.0,
    )


def _apply_scenario(state: LiveRiskState, scenario: SizingStressScenario) -> LiveRiskState:
    return replace(
        state,
        capacity=_stressed_capacity(state.capacity, scenario.capacity_multiplier),
        daily_loss_fraction=state.daily_loss_fraction + scenario.daily_loss_add,
        current_drawdown_fraction=state.current_drawdown_fraction + scenario.drawdown_add,
        gross_exposure_fraction=state.gross_exposure_fraction + scenario.gross_exposure_add,
        cluster_exposure_fraction=state.cluster_exposure_fraction + scenario.cluster_exposure_add,
        quote_age_ms=state.quote_age_ms * scenario.quote_age_multiplier,
        decision_latency_ms=state.decision_latency_ms * scenario.decision_latency_multiplier,
        quote_consensus_ok=(state.quote_consensus_ok and not scenario.force_quote_consensus_bad),
        runtime_certified=(state.runtime_certified and not scenario.force_runtime_uncertified),
        canary_healthy=(state.canary_healthy and not scenario.force_canary_unhealthy),
        drift_active=(state.drift_active or scenario.force_drift_active),
    )


def stress_test_live_sizing_envelope(
    request: LiveSizingRequest,
    state: LiveRiskState,
    *,
    policy: LiveSizingSafetyPolicy,
    scenarios: tuple[SizingStressScenario, ...] | None = None,
) -> SizingStressReport:
    """Stress the sizing guard and enforce monotonic risk behavior under worsening states."""
    scenarios = scenarios or default_sizing_stress_scenarios()
    baseline = evaluate_live_sizing_request(request, state, policy=policy)
    points: list[SizingStressPoint] = []
    unsafe_permits: list[str] = []
    monotonicity: list[str] = []

    for scenario in scenarios:
        stressed = _apply_scenario(state, scenario)
        decision = evaluate_live_sizing_request(request, stressed, policy=policy)
        more_permissive = decision.hard_ceiling_fraction > baseline.hard_ceiling_fraction + 1e-15
        if more_permissive:
            monotonicity.append(scenario.name)
        blocked_to_allowed = (
            baseline.status is LiveSizingStatus.BLOCKED
            and decision.status is LiveSizingStatus.WITHIN_OPERATOR_LIMITS
        )
        if blocked_to_allowed:
            unsafe_permits.append(scenario.name)
        points.append(
            SizingStressPoint(
                scenario=scenario.name,
                decision=decision,
                stressed_hard_ceiling_fraction=decision.hard_ceiling_fraction,
                became_more_permissive=more_permissive,
            )
        )

    return SizingStressReport(
        baseline=baseline,
        points=tuple(points),
        blocked_scenarios=sum(item.decision.status is LiveSizingStatus.BLOCKED for item in points),
        unsafe_permits=tuple(unsafe_permits),
        monotonicity_violations=tuple(monotonicity),
        passed=not unsafe_permits and not monotonicity,
    )

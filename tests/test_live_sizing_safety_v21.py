from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scorpion.fail_safe_control import SafetyLatchState, SafetyMode
from scorpion.liquidity_capacity import LiquidityCapacityReport
from scorpion.live_sizing_safety import (
    LiveRiskState,
    LiveSizingRequest,
    LiveSizingSafetyPolicy,
    LiveSizingStatus,
    evaluate_live_sizing_request,
)
from scorpion.sizing_lab import RiskSimulation, SizingEnvelope, SizingReadiness

NOW = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)


def _policy() -> LiveSizingSafetyPolicy:
    return LiveSizingSafetyPolicy(
        maximum_risk_fraction=0.02,
        capacity_reference_risk_fraction=0.01,
        maximum_daily_loss_fraction=0.03,
        maximum_drawdown_fraction=0.10,
        maximum_gross_exposure_fraction=0.50,
        maximum_cluster_exposure_fraction=0.20,
        maximum_quote_age_ms=500.0,
        maximum_decision_latency_ms=750.0,
        decision_ttl=timedelta(seconds=2),
    )


def _sizing() -> SizingEnvelope:
    selected = RiskSimulation(
        risk_fraction=0.02,
        ruin_probability=0.0,
        drawdown_breach_probability=0.01,
        median_terminal_equity=1.20,
        p05_terminal_equity=0.95,
        median_max_drawdown=0.04,
    )
    return SizingEnvelope(
        segment="all",
        readiness=SizingReadiness.READY_FOR_RESEARCH,
        max_research_risk_fraction=0.02,
        selected_simulation=selected,
        simulations=(selected,),
        reason="research envelope",
    )


def _capacity() -> LiquidityCapacityReport:
    return LiquidityCapacityReport(
        scenarios=(),
        frontier=(),
        max_robust_clip_multiplier=2.0,
        robust=True,
    )


def _safety(mode: SafetyMode = SafetyMode.NORMAL) -> SafetyLatchState:
    return SafetyLatchState(
        component="entry-model",
        mode=mode,
        source_release_id="release-v21",
        reason="healthy" if mode is SafetyMode.NORMAL else "fault injected",
        updated_ts_utc=NOW,
        updated_by="safety-monitor",
    )


def _state() -> LiveRiskState:
    return LiveRiskState(
        observed_ts_utc=NOW,
        safety_state=_safety(),
        research_sizing=_sizing(),
        capacity=_capacity(),
        daily_loss_fraction=0.005,
        current_drawdown_fraction=0.02,
        gross_exposure_fraction=0.10,
        cluster_exposure_fraction=0.05,
        quote_age_ms=50.0,
        decision_latency_ms=100.0,
        quote_consensus_ok=True,
        runtime_certified=True,
        canary_healthy=True,
        drift_active=False,
        active_release_id="release-v21",
    )


def _request(risk: float = 0.01, *, created: datetime = NOW) -> LiveSizingRequest:
    return LiveSizingRequest(
        request_id="operator-request-v21",
        component="entry-model",
        release_id="release-v21",
        operator="operator-risk",
        requested_risk_fraction=risk,
        created_ts_utc=created,
    )


def test_live_sizing_guard_validates_operator_selected_risk_without_selecting_size():
    decision = evaluate_live_sizing_request(_request(), _state(), policy=_policy())
    assert decision.status is LiveSizingStatus.WITHIN_OPERATOR_LIMITS
    assert decision.permitted is True
    assert decision.requested_risk_fraction == 0.01
    assert decision.hard_ceiling_fraction == 0.02
    assert decision.capacity_implied_ceiling_fraction == 0.02


def test_capacity_frontier_directly_caps_live_risk_ceiling():
    state = replace(
        _state(),
        capacity=LiquidityCapacityReport(
            scenarios=(),
            frontier=(),
            max_robust_clip_multiplier=1.25,
            robust=True,
        ),
    )
    decision = evaluate_live_sizing_request(_request(0.015), state, policy=_policy())
    assert decision.status is LiveSizingStatus.BLOCKED
    assert decision.hard_ceiling_fraction == 0.0125
    assert any(item.startswith("requested_risk_fraction") for item in decision.failures)


def test_adversarial_no_trade_drift_and_market_data_faults_all_fail_closed():
    baseline = _state()
    injected = (
        (replace(baseline, safety_state=_safety(SafetyMode.NO_TRADE)), "component_fail_closed"),
        (replace(baseline, drift_active=True), "drift_active"),
        (replace(baseline, quote_consensus_ok=False), "quote_consensus_unhealthy"),
        (replace(baseline, runtime_certified=False), "runtime_not_certified"),
        (replace(baseline, canary_healthy=False), "live_canary_unhealthy"),
        (replace(baseline, current_drawdown_fraction=0.10), "drawdown_fraction"),
        (replace(baseline, daily_loss_fraction=0.03), "daily_loss_fraction"),
        (replace(baseline, quote_age_ms=501.0), "quote_age_ms"),
        (replace(baseline, decision_latency_ms=751.0), "decision_latency_ms"),
    )
    for state, expected in injected:
        decision = evaluate_live_sizing_request(_request(), state, policy=_policy())
        assert decision.status is LiveSizingStatus.BLOCKED
        assert any(expected in failure for failure in decision.failures)


def test_stale_operator_sizing_request_fails_closed():
    request = _request(created=NOW - timedelta(seconds=3))
    decision = evaluate_live_sizing_request(request, _state(), policy=_policy())
    assert decision.status is LiveSizingStatus.BLOCKED
    assert any(item.startswith("sizing_request_stale_ms") for item in decision.failures)


def test_sizing_decision_identity_changes_when_risk_state_changes():
    first = evaluate_live_sizing_request(_request(), _state(), policy=_policy())
    second = evaluate_live_sizing_request(
        _request(),
        replace(_state(), gross_exposure_fraction=0.49),
        policy=_policy(),
    )
    assert first.decision_id != second.decision_id


def test_live_sizing_rejects_stale_release_identity_and_reserves_exposure_headroom():
    mismatched = evaluate_live_sizing_request(
        _request(),
        replace(_state(), active_release_id="release-new"),
        policy=_policy(),
    )
    assert mismatched.status is LiveSizingStatus.BLOCKED
    assert "sizing_request_release_mismatch" in mismatched.failures

    tight = evaluate_live_sizing_request(
        _request(0.01),
        replace(_state(), gross_exposure_fraction=0.495),
        policy=_policy(),
    )
    assert tight.hard_ceiling_fraction == pytest.approx(0.005)
    assert tight.portfolio_headroom_ceiling_fraction == pytest.approx(0.005)
    assert tight.status is LiveSizingStatus.BLOCKED

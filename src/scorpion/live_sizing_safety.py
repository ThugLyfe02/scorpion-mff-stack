from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .fail_safe_control import SafetyLatchState, SafetyMode
from .liquidity_capacity import LiquidityCapacityReport
from .sizing_lab import SizingEnvelope, SizingReadiness


class LiveSizingStatus(StrEnum):
    BLOCKED = "BLOCKED"
    WITHIN_OPERATOR_LIMITS = "WITHIN_OPERATOR_LIMITS"


@dataclass(frozen=True, slots=True)
class LiveSizingSafetyPolicy:
    maximum_risk_fraction: float
    capacity_reference_risk_fraction: float
    maximum_daily_loss_fraction: float
    maximum_drawdown_fraction: float
    maximum_gross_exposure_fraction: float
    maximum_cluster_exposure_fraction: float
    maximum_quote_age_ms: float
    maximum_decision_latency_ms: float
    decision_ttl: timedelta = timedelta(seconds=2)

    def __post_init__(self) -> None:
        for name in (
            "maximum_risk_fraction",
            "capacity_reference_risk_fraction",
            "maximum_daily_loss_fraction",
            "maximum_drawdown_fraction",
            "maximum_gross_exposure_fraction",
            "maximum_cluster_exposure_fraction",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be finite and in (0,1]")
        for name in ("maximum_quote_age_ms", "maximum_decision_latency_ms"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.decision_ttl <= timedelta(0):
            raise ValueError("decision_ttl must be positive")


@dataclass(frozen=True, slots=True)
class LiveSizingRequest:
    request_id: str
    component: str
    release_id: str
    operator: str
    requested_risk_fraction: float
    created_ts_utc: datetime

    def __post_init__(self) -> None:
        identities = (
            self.request_id,
            self.component,
            self.release_id,
            self.operator,
        )
        if not all(value.strip() for value in identities):
            raise ValueError("request, component, release and operator identities are required")
        if not math.isfinite(self.requested_risk_fraction) or self.requested_risk_fraction <= 0:
            raise ValueError("requested_risk_fraction must be finite and positive")
        if self.created_ts_utc.tzinfo is None or self.created_ts_utc.utcoffset() is None:
            raise ValueError("created_ts_utc must be timezone-aware")


@dataclass(frozen=True, slots=True)
class LiveRiskState:
    observed_ts_utc: datetime
    safety_state: SafetyLatchState
    research_sizing: SizingEnvelope
    capacity: LiquidityCapacityReport
    daily_loss_fraction: float
    current_drawdown_fraction: float
    gross_exposure_fraction: float
    cluster_exposure_fraction: float
    quote_age_ms: float
    decision_latency_ms: float
    quote_consensus_ok: bool
    runtime_certified: bool
    canary_healthy: bool
    drift_active: bool

    def __post_init__(self) -> None:
        if self.observed_ts_utc.tzinfo is None or self.observed_ts_utc.utcoffset() is None:
            raise ValueError("observed_ts_utc must be timezone-aware")
        for name in (
            "daily_loss_fraction",
            "current_drawdown_fraction",
            "gross_exposure_fraction",
            "cluster_exposure_fraction",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        for name in ("quote_age_ms", "decision_latency_ms"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class LiveSizingDecision:
    decision_id: str
    request_id: str
    status: LiveSizingStatus
    requested_risk_fraction: float
    hard_ceiling_fraction: float
    capacity_implied_ceiling_fraction: float
    valid_until_ts_utc: datetime
    failures: tuple[str, ...]

    @property
    def permitted(self) -> bool:
        return self.status is LiveSizingStatus.WITHIN_OPERATOR_LIMITS


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evaluate_live_sizing_request(
    request: LiveSizingRequest,
    state: LiveRiskState,
    *,
    policy: LiveSizingSafetyPolicy,
) -> LiveSizingDecision:
    """Validate an operator-selected risk fraction against hard safety ceilings.

    This function never chooses a position size, contract count, or dollar allocation. It only
    determines whether the operator's already-selected risk fraction is inside independently
    configured research, capacity, portfolio, market-data, and fail-closed limits.
    """
    observed = state.observed_ts_utc.astimezone(UTC)
    created = request.created_ts_utc.astimezone(UTC)
    failures: list[str] = []

    if request.component != state.safety_state.component:
        failures.append("safety_latch_component_mismatch")
    if state.safety_state.mode is not SafetyMode.NORMAL:
        failures.append("component_fail_closed_no_trade")
    if state.research_sizing.readiness is not SizingReadiness.READY_FOR_RESEARCH:
        failures.append("research_sizing_not_ready")
    if state.research_sizing.selected_simulation is None:
        failures.append("research_sizing_has_no_selected_simulation")
    if not state.capacity.robust:
        failures.append("liquidity_capacity_not_robust")
    if state.drift_active:
        failures.append("model_or_policy_drift_active")
    if not state.quote_consensus_ok:
        failures.append("quote_consensus_unhealthy")
    if not state.runtime_certified:
        failures.append("runtime_not_certified")
    if not state.canary_healthy:
        failures.append("live_canary_unhealthy")

    age = observed - created
    if age < timedelta(0):
        failures.append("sizing_request_timestamp_in_future")
    elif age > policy.decision_ttl:
        failures.append(
            f"sizing_request_stale_ms:{age.total_seconds() * 1000:.3f}"
            f">{policy.decision_ttl.total_seconds() * 1000:.3f}"
        )

    if state.daily_loss_fraction >= policy.maximum_daily_loss_fraction:
        failures.append(
            f"daily_loss_fraction:{state.daily_loss_fraction:.6f}"
            f">={policy.maximum_daily_loss_fraction:.6f}"
        )
    if state.current_drawdown_fraction >= policy.maximum_drawdown_fraction:
        failures.append(
            f"drawdown_fraction:{state.current_drawdown_fraction:.6f}"
            f">={policy.maximum_drawdown_fraction:.6f}"
        )
    if state.gross_exposure_fraction >= policy.maximum_gross_exposure_fraction:
        failures.append(
            f"gross_exposure_fraction:{state.gross_exposure_fraction:.6f}"
            f">={policy.maximum_gross_exposure_fraction:.6f}"
        )
    if state.cluster_exposure_fraction >= policy.maximum_cluster_exposure_fraction:
        failures.append(
            f"cluster_exposure_fraction:{state.cluster_exposure_fraction:.6f}"
            f">={policy.maximum_cluster_exposure_fraction:.6f}"
        )
    if state.quote_age_ms > policy.maximum_quote_age_ms:
        failures.append(
            f"quote_age_ms:{state.quote_age_ms:.3f}>{policy.maximum_quote_age_ms:.3f}"
        )
    if state.decision_latency_ms > policy.maximum_decision_latency_ms:
        failures.append(
            "decision_latency_ms:"
            f"{state.decision_latency_ms:.3f}>{policy.maximum_decision_latency_ms:.3f}"
        )

    capacity_ceiling = (
        policy.capacity_reference_risk_fraction * state.capacity.max_robust_clip_multiplier
        if state.capacity.robust
        else 0.0
    )
    hard_ceiling = min(
        policy.maximum_risk_fraction,
        state.research_sizing.max_research_risk_fraction,
        capacity_ceiling,
    )
    if hard_ceiling <= 0:
        failures.append("no_positive_live_risk_ceiling")
    if request.requested_risk_fraction > hard_ceiling:
        failures.append(
            f"requested_risk_fraction:{request.requested_risk_fraction:.6f}"
            f">hard_ceiling:{hard_ceiling:.6f}"
        )

    valid_until = request.created_ts_utc.astimezone(UTC) + policy.decision_ttl
    status = LiveSizingStatus.WITHIN_OPERATOR_LIMITS if not failures else LiveSizingStatus.BLOCKED
    decision_id = _hash_payload(
        {
            "version": "live-sizing-safety-v1",
            "request": asdict(request),
            "state": asdict(state),
            "policy": asdict(policy),
            "hard_ceiling_fraction": hard_ceiling,
            "capacity_implied_ceiling_fraction": capacity_ceiling,
            "status": status.value,
            "failures": failures,
        }
    )
    return LiveSizingDecision(
        decision_id=decision_id,
        request_id=request.request_id,
        status=status,
        requested_risk_fraction=request.requested_risk_fraction,
        hard_ceiling_fraction=hard_ceiling,
        capacity_implied_ceiling_fraction=capacity_ceiling,
        valid_until_ts_utc=valid_until,
        failures=tuple(failures),
    )

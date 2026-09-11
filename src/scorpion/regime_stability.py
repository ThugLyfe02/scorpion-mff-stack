from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from .execution_forensics import CompletedTrade
from .microstructure_forensics import MicroForensicLeg

_MARKET_TZ = ZoneInfo("America/New_York")
_BPS = Decimal("10000")


class RegimeStabilityStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class RegimeStabilityPolicy:
    minimum_total_samples: int = 30
    minimum_group_samples: int = 5
    minimum_qualified_groups_per_axis: int = 2
    minimum_positive_group_ratio: float = 0.60
    maximum_dominant_group_share: float = 0.85
    tight_spread_bps: float = 250.0
    medium_spread_bps: float = 750.0
    fresh_quote_age_ms: float = 50.0
    normal_quote_age_ms: float = 250.0
    fast_decision_latency_ms: float = 100.0
    normal_decision_latency_ms: float = 500.0

    def __post_init__(self) -> None:
        if self.minimum_total_samples <= 0 or self.minimum_group_samples <= 0:
            raise ValueError("sample thresholds must be positive")
        if self.minimum_qualified_groups_per_axis <= 0:
            raise ValueError("minimum_qualified_groups_per_axis must be positive")
        if not 0 <= self.minimum_positive_group_ratio <= 1:
            raise ValueError("minimum_positive_group_ratio must be in [0,1]")
        if not 0 < self.maximum_dominant_group_share <= 1:
            raise ValueError("maximum_dominant_group_share must be in (0,1]")


@dataclass(frozen=True, slots=True)
class RegimeGroup:
    axis: str
    bucket: str
    samples: int
    mean_return: float
    win_rate: float
    sample_share: float
    qualified: bool

    @property
    def positive(self) -> bool:
        return self.mean_return > 0


@dataclass(frozen=True, slots=True)
class RegimeAxisReport:
    axis: str
    groups: tuple[RegimeGroup, ...]
    qualified_groups: int
    positive_qualified_groups: int
    positive_group_ratio: float
    dominant_group_share: float
    status: RegimeStabilityStatus


@dataclass(frozen=True, slots=True)
class RegimeStabilityReport:
    total_samples: int
    axes: tuple[RegimeAxisReport, ...]
    status: RegimeStabilityStatus
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.status is RegimeStabilityStatus.PASS


def _spread_bps(leg: MicroForensicLeg) -> float | None:
    bid = leg.decision_quote_bid
    ask = leg.decision_quote_ask
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    midpoint = (bid + ask) / Decimal("2")
    if midpoint <= 0:
        return None
    return float(((ask - bid) / midpoint) * _BPS)


def _quote_age_ms(leg: MicroForensicLeg) -> float | None:
    if leg.decision_ts_utc is None or leg.decision_quote_recv_ns is None:
        return None
    decision_ns = int(leg.decision_ts_utc.timestamp() * 1_000_000_000)
    return (decision_ns - leg.decision_quote_recv_ns) / 1_000_000.0


def _decision_latency_ms(leg: MicroForensicLeg) -> float | None:
    if leg.decision_ts_utc is None:
        return None
    return (leg.decision_ts_utc - leg.source_ts_utc).total_seconds() * 1000.0


def _session_bucket(leg: MicroForensicLeg) -> str:
    local = leg.source_ts_utc.astimezone(_MARKET_TZ).time()
    if time(9, 30) <= local < time(10, 30):
        return "OPEN"
    if time(10, 30) <= local < time(15, 0):
        return "MIDDAY"
    if time(15, 0) <= local <= time(16, 0):
        return "CLOSE"
    return "OUTSIDE_RTH"


def _bucket_spread(value: float | None, policy: RegimeStabilityPolicy) -> str:
    if value is None:
        return "UNKNOWN"
    if value <= policy.tight_spread_bps:
        return "TIGHT"
    if value <= policy.medium_spread_bps:
        return "MEDIUM"
    return "WIDE"


def _bucket_quote_age(value: float | None, policy: RegimeStabilityPolicy) -> str:
    if value is None or value < 0:
        return "UNKNOWN"
    if value <= policy.fresh_quote_age_ms:
        return "FRESH"
    if value <= policy.normal_quote_age_ms:
        return "NORMAL"
    return "STALE"


def _bucket_latency(value: float | None, policy: RegimeStabilityPolicy) -> str:
    if value is None or value < 0:
        return "UNKNOWN"
    if value <= policy.fast_decision_latency_ms:
        return "FAST"
    if value <= policy.normal_decision_latency_ms:
        return "NORMAL"
    return "SLOW"


def _axis_report(
    axis: str,
    assignments: list[tuple[str, Decimal]],
    *,
    total_samples: int,
    policy: RegimeStabilityPolicy,
) -> RegimeAxisReport:
    grouped: dict[str, list[Decimal]] = {}
    for bucket, value in assignments:
        grouped.setdefault(bucket, []).append(value)
    groups: list[RegimeGroup] = []
    for bucket in sorted(grouped):
        returns = grouped[bucket]
        samples = len(returns)
        groups.append(
            RegimeGroup(
                axis=axis,
                bucket=bucket,
                samples=samples,
                mean_return=float(sum(returns, Decimal("0")) / samples),
                win_rate=sum(value > 0 for value in returns) / samples,
                sample_share=samples / total_samples if total_samples else 0.0,
                qualified=samples >= policy.minimum_group_samples,
            )
        )
    qualified = [group for group in groups if group.qualified and group.bucket != "UNKNOWN"]
    positive = [group for group in qualified if group.positive]
    positive_ratio = len(positive) / len(qualified) if qualified else 0.0
    dominant_share = max((group.sample_share for group in groups), default=0.0)
    if len(qualified) < policy.minimum_qualified_groups_per_axis:
        status = RegimeStabilityStatus.INSUFFICIENT
    elif (
        positive_ratio < policy.minimum_positive_group_ratio
        or dominant_share > policy.maximum_dominant_group_share
    ):
        status = RegimeStabilityStatus.FAIL
    else:
        status = RegimeStabilityStatus.PASS
    return RegimeAxisReport(
        axis=axis,
        groups=tuple(groups),
        qualified_groups=len(qualified),
        positive_qualified_groups=len(positive),
        positive_group_ratio=positive_ratio,
        dominant_group_share=dominant_share,
        status=status,
    )


def evaluate_regime_stability(
    trades: tuple[CompletedTrade, ...],
    legs: tuple[MicroForensicLeg, ...],
    *,
    policy: RegimeStabilityPolicy | None = None,
) -> RegimeStabilityReport:
    policy = policy or RegimeStabilityPolicy()
    if len(trades) < policy.minimum_total_samples:
        return RegimeStabilityReport(
            total_samples=len(trades),
            axes=(),
            status=RegimeStabilityStatus.INSUFFICIENT,
            failures=(
                f"insufficient_total_samples:{len(trades)}<{policy.minimum_total_samples}",
            ),
        )

    entry_legs = {leg.event_id: leg for leg in legs if leg.event_kind.value == "ENTRY"}
    spread_assignments: list[tuple[str, Decimal]] = []
    age_assignments: list[tuple[str, Decimal]] = []
    latency_assignments: list[tuple[str, Decimal]] = []
    session_assignments: list[tuple[str, Decimal]] = []
    missing_entry_evidence = 0

    for trade in trades:
        leg = entry_legs.get(trade.entry_event_id)
        if leg is None:
            missing_entry_evidence += 1
            continue
        value = trade.return_fraction
        spread_assignments.append((_bucket_spread(_spread_bps(leg), policy), value))
        age_assignments.append((_bucket_quote_age(_quote_age_ms(leg), policy), value))
        latency_assignments.append((_bucket_latency(_decision_latency_ms(leg), policy), value))
        session_assignments.append((_session_bucket(leg), value))

    effective_samples = len(trades) - missing_entry_evidence
    if effective_samples < policy.minimum_total_samples:
        return RegimeStabilityReport(
            total_samples=effective_samples,
            axes=(),
            status=RegimeStabilityStatus.INSUFFICIENT,
            failures=(f"missing_entry_microstructure_evidence:{missing_entry_evidence}",),
        )

    axes = (
        _axis_report("spread", spread_assignments, total_samples=effective_samples, policy=policy),
        _axis_report("quote_age", age_assignments, total_samples=effective_samples, policy=policy),
        _axis_report(
            "decision_latency",
            latency_assignments,
            total_samples=effective_samples,
            policy=policy,
        ),
        _axis_report("session", session_assignments, total_samples=effective_samples, policy=policy),
    )
    failures = tuple(
        f"regime_axis_{axis.axis}:{axis.status.value}"
        for axis in axes
        if axis.status is not RegimeStabilityStatus.PASS
    )
    status = RegimeStabilityStatus.PASS if not failures else RegimeStabilityStatus.FAIL
    return RegimeStabilityReport(effective_samples, axes, status, failures)

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from .domain import EventKind
from .execution_forensics import CompletedTrade
from .microstructure import OptionMicrostructureTape
from .microstructure_forensics import MicroForensicLeg, MicrostructureForensicsReport


class CapacityStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class LiquidityCapacityPolicy:
    clip_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0, 3.0)
    displayed_depth_haircuts: tuple[float, ...] = (0.25, 0.50, 0.75, 1.0)
    minimum_supported_trades: int = 20
    minimum_lifecycle_coverage: float = 0.80

    def __post_init__(self) -> None:
        if not self.clip_multipliers or any(value <= 0 for value in self.clip_multipliers):
            raise ValueError("clip multipliers must be positive")
        if any(value <= 0 or value > 1 for value in self.displayed_depth_haircuts):
            raise ValueError("depth haircuts must be in (0,1]")
        if self.minimum_supported_trades <= 0:
            raise ValueError("minimum_supported_trades must be positive")
        if not 0 <= self.minimum_lifecycle_coverage <= 1:
            raise ValueError("minimum_lifecycle_coverage must be in [0,1]")


@dataclass(frozen=True, slots=True)
class CapacityScenario:
    clip_multiplier: float
    depth_haircut: float
    total_trades: int
    supported_trades: int
    lifecycle_coverage: float
    mean_return: float
    status: CapacityStatus


@dataclass(frozen=True, slots=True)
class CapacityFrontierPoint:
    clip_multiplier: float
    worst_depth_haircut: float
    worst_lifecycle_coverage: float
    minimum_supported_trades: int
    worst_mean_return: float
    robust: bool


@dataclass(frozen=True, slots=True)
class LiquidityCapacityReport:
    scenarios: tuple[CapacityScenario, ...]
    frontier: tuple[CapacityFrontierPoint, ...]
    max_robust_clip_multiplier: float
    robust: bool


def _side_depth(leg: MicroForensicLeg, tape: OptionMicrostructureTape) -> int | None:
    if leg.contract_key is None or leg.fill_evidence_event_ns is None:
        return None
    quote = tape.market_quote_at(
        leg.contract_key,
        leg.fill_evidence_event_ns,
        max_age=timedelta(seconds=1),
    )
    if quote is None:
        return None
    if leg.event_kind in {EventKind.ENTRY, EventKind.ADD}:
        return quote.ask_size
    if leg.event_kind in {EventKind.TRIM, EventKind.EXIT}:
        return quote.bid_size
    return None


def _trade_legs(
    trade: CompletedTrade,
    legs: tuple[MicroForensicLeg, ...],
) -> tuple[MicroForensicLeg, ...]:
    return tuple(
        leg
        for leg in legs
        if leg.contract_key == trade.contract_key
        and trade.opened_ts_utc <= leg.source_ts_utc <= trade.closed_ts_utc
        and leg.event_kind in {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT}
        and leg.requested_quantity > 0
        and leg.conservative_fill_quantity > 0
    )


def _supports_trade(
    trade: CompletedTrade,
    legs: tuple[MicroForensicLeg, ...],
    tape: OptionMicrostructureTape,
    *,
    clip_multiplier: float,
    depth_haircut: float,
) -> bool:
    lifecycle_legs = _trade_legs(trade, legs)
    if not lifecycle_legs:
        return False
    for leg in lifecycle_legs:
        depth = _side_depth(leg, tape)
        if depth is None:
            return False
        usable_depth = math.floor(depth * depth_haircut)
        required = math.ceil(leg.requested_quantity * clip_multiplier)
        if usable_depth < required:
            return False
    return True


def evaluate_liquidity_capacity(
    report: MicrostructureForensicsReport,
    tape: OptionMicrostructureTape,
    *,
    policy: LiquidityCapacityPolicy | None = None,
) -> LiquidityCapacityReport:
    policy = policy or LiquidityCapacityPolicy()
    trades = tuple(
        trade for trade in report.completed_trades if trade.depth_evidence_complete
    )
    scenarios: list[CapacityScenario] = []
    for multiplier in policy.clip_multipliers:
        for haircut in policy.displayed_depth_haircuts:
            supported = tuple(
                trade
                for trade in trades
                if _supports_trade(
                    trade,
                    report.legs,
                    tape,
                    clip_multiplier=multiplier,
                    depth_haircut=haircut,
                )
            )
            coverage = len(supported) / len(trades) if trades else 0.0
            mean_return = (
                statistics.fmean(float(trade.return_fraction) for trade in supported)
                if supported
                else 0.0
            )
            if len(trades) < policy.minimum_supported_trades:
                status = CapacityStatus.INSUFFICIENT
            elif (
                len(supported) < policy.minimum_supported_trades
                or coverage < policy.minimum_lifecycle_coverage
            ):
                status = CapacityStatus.FAIL
            else:
                status = CapacityStatus.PASS
            scenarios.append(
                CapacityScenario(
                    clip_multiplier=multiplier,
                    depth_haircut=haircut,
                    total_trades=len(trades),
                    supported_trades=len(supported),
                    lifecycle_coverage=coverage,
                    mean_return=mean_return,
                    status=status,
                )
            )

    frontier: list[CapacityFrontierPoint] = []
    robust_multipliers: list[float] = []
    for multiplier in policy.clip_multipliers:
        relevant = [item for item in scenarios if item.clip_multiplier == multiplier]
        if not relevant:
            continue
        worst = min(relevant, key=lambda item: (item.lifecycle_coverage, item.supported_trades))
        minimum_supported = min(item.supported_trades for item in relevant)
        worst_mean = min(item.mean_return for item in relevant)
        robust = all(item.status is CapacityStatus.PASS for item in relevant)
        if robust:
            robust_multipliers.append(multiplier)
        frontier.append(
            CapacityFrontierPoint(
                clip_multiplier=multiplier,
                worst_depth_haircut=worst.depth_haircut,
                worst_lifecycle_coverage=worst.lifecycle_coverage,
                minimum_supported_trades=minimum_supported,
                worst_mean_return=worst_mean,
                robust=robust,
            )
        )
    maximum = max(robust_multipliers, default=0.0)
    return LiquidityCapacityReport(
        scenarios=tuple(scenarios),
        frontier=tuple(frontier),
        max_robust_clip_multiplier=maximum,
        robust=maximum > 0,
    )

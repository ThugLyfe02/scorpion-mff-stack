from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from .contract_terms import ContractTermsRegistry
from .domain import EventKind
from .microstructure import ns_from_datetime
from .microstructure_forensics import MicroForensicLeg


@dataclass(frozen=True, slots=True)
class TickAlignmentIssue:
    event_id: str
    contract_key: str
    raw_limit: Decimal
    min_price_increment: Decimal | None
    rounded_down_limit: Decimal | None
    reason: str


@dataclass(frozen=True, slots=True)
class TickAlignmentReport:
    evaluated_limits: int
    aligned_limits: int
    missing_terms: int
    missing_increment: int
    misaligned_limits: int
    issues: tuple[TickAlignmentIssue, ...]

    @property
    def all_aligned(self) -> bool:
        return (
            self.evaluated_limits > 0
            and self.aligned_limits == self.evaluated_limits
            and not self.issues
        )


def round_buy_limit_down(price: Decimal, increment: Decimal) -> Decimal | None:
    if price <= 0 or increment <= 0:
        raise ValueError("price and increment must be positive")
    ticks = (price / increment).to_integral_value(rounding=ROUND_FLOOR)
    rounded = ticks * increment
    return rounded if rounded > 0 else None


def is_tick_aligned(price: Decimal, increment: Decimal) -> bool:
    if price <= 0 or increment <= 0:
        return False
    return price % increment == 0


def validate_buy_limit_ticks(
    legs: tuple[MicroForensicLeg, ...],
    registry: ContractTermsRegistry,
) -> TickAlignmentReport:
    actionable = tuple(
        leg
        for leg in legs
        if leg.event_kind in {EventKind.ENTRY, EventKind.ADD}
        and leg.contract_key is not None
        and leg.limit_price is not None
    )
    aligned = 0
    missing_terms = 0
    missing_increment = 0
    misaligned = 0
    issues: list[TickAlignmentIssue] = []

    for leg in actionable:
        if leg.contract_key is None or leg.limit_price is None:
            continue
        terms = registry.resolve(leg.contract_key, ns_from_datetime(leg.source_ts_utc))
        if terms is None:
            missing_terms += 1
            issues.append(
                TickAlignmentIssue(
                    event_id=leg.event_id,
                    contract_key=leg.contract_key,
                    raw_limit=leg.limit_price,
                    min_price_increment=None,
                    rounded_down_limit=None,
                    reason="point-in-time contract terms are missing or ambiguous",
                )
            )
            continue
        increment = terms.min_price_increment
        if increment is None:
            missing_increment += 1
            issues.append(
                TickAlignmentIssue(
                    event_id=leg.event_id,
                    contract_key=leg.contract_key,
                    raw_limit=leg.limit_price,
                    min_price_increment=None,
                    rounded_down_limit=None,
                    reason="minimum price increment is unavailable",
                )
            )
            continue
        if is_tick_aligned(leg.limit_price, increment):
            aligned += 1
            continue
        misaligned += 1
        issues.append(
            TickAlignmentIssue(
                event_id=leg.event_id,
                contract_key=leg.contract_key,
                raw_limit=leg.limit_price,
                min_price_increment=increment,
                rounded_down_limit=round_buy_limit_down(leg.limit_price, increment),
                reason="modeled BUY limit is not venue-tick aligned",
            )
        )

    return TickAlignmentReport(
        evaluated_limits=len(actionable),
        aligned_limits=aligned,
        missing_terms=missing_terms,
        missing_increment=missing_increment,
        misaligned_limits=misaligned,
        issues=tuple(issues),
    )

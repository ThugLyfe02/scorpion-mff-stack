from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from .domain import BookState, Effect, SignalEvent
from .reducer import reduce_book


class IntrabarPolicy(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    OPTIMISTIC = "OPTIMISTIC"
    BOUNDS = "BOUNDS"


@dataclass(frozen=True, slots=True)
class Bar:
    begins_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class AmbiguousFill:
    optimistic: Decimal | None
    conservative: Decimal | None
    reason: str


def same_bar_target_resolution(
    *,
    entry_price: Decimal,
    target_price: Decimal,
    bar: Bar,
    entry_occurs_within_bar: bool,
) -> AmbiguousFill:
    """Never silently assumes a favorable OHLC ordering on the entry bar."""
    if not entry_occurs_within_bar:
        hit = bar.high >= target_price if target_price >= entry_price else bar.low <= target_price
        px = target_price if hit else None
        return AmbiguousFill(px, px, "ordered_bar")

    if target_price >= entry_price and bar.high >= target_price:
        return AmbiguousFill(target_price, None, "entry_bar_high_order_unknown")
    if target_price < entry_price and bar.low <= target_price:
        return AmbiguousFill(target_price, None, "entry_bar_low_order_unknown")
    return AmbiguousFill(None, None, "target_not_printed")


def replay(events: Sequence[SignalEvent]) -> tuple[BookState, tuple[Effect, ...]]:
    ordered = sorted(events, key=lambda e: (e.source_ts_utc, e.received_ts_utc, e.event_id))
    state = BookState()
    effects: list[Effect] = []
    for event in ordered:
        state, produced = reduce_book(state, event)
        effects.extend(produced)
    return state, tuple(effects)


def realized_cost_basis_after_reduction(
    *,
    average_price: Decimal,
    starting_quantity: int,
    sold_quantity: int,
) -> tuple[int, Decimal]:
    if not 0 <= sold_quantity <= starting_quantity:
        raise ValueError("invalid sold quantity")
    remaining = starting_quantity - sold_quantity
    # Average cost of remaining fungible long contracts is unchanged by a sale.
    return remaining, average_price if remaining else Decimal("0")

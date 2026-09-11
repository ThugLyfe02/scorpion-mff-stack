from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .config import DEFAULT_POLICY, Policy
from .domain import BookState, Effect, SignalEvent
from .reducer import reduce_book


class IntrabarPolicy(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    OPTIMISTIC = "OPTIMISTIC"
    BOUNDS = "BOUNDS"


class ReplayOrder(StrEnum):
    SOURCE_TIME = "SOURCE_TIME"
    INPUT = "INPUT"


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
    """Never silently assume favorable OHLC ordering on the entry bar."""
    if not entry_occurs_within_bar:
        hit = bar.high >= target_price if target_price >= entry_price else bar.low <= target_price
        px = target_price if hit else None
        return AmbiguousFill(px, px, "ordered_bar")

    if target_price >= entry_price and bar.high >= target_price:
        return AmbiguousFill(target_price, None, "entry_bar_high_order_unknown")
    if target_price < entry_price and bar.low <= target_price:
        return AmbiguousFill(target_price, None, "entry_bar_low_order_unknown")
    return AmbiguousFill(None, None, "target_not_printed")


def replay(
    events: Sequence[SignalEvent],
    policy: Policy = DEFAULT_POLICY,
    *,
    order: ReplayOrder = ReplayOrder.SOURCE_TIME,
) -> tuple[BookState, tuple[Effect, ...]]:
    """Replay normalized events under an explicit ordering contract.

    SOURCE_TIME preserves the original research/counterfactual behavior. INPUT is for runtime and
    recovery surfaces whose caller has already loaded events in the durable normalized process
    sequence. Stateful production replay must not silently re-sort that sequence by source clock.
    """
    if order is ReplayOrder.SOURCE_TIME:
        ordered: Sequence[SignalEvent] = sorted(
            events,
            key=lambda event: (event.source_ts_utc, event.received_ts_utc, event.event_id),
        )
    else:
        ordered = events
    state = BookState()
    effects: list[Effect] = []
    for event in ordered:
        state, produced = reduce_book(state, event, policy)
        effects.extend(produced)
    return state, tuple(effects)


def state_fingerprint(state: BookState) -> str:
    """Stable digest used to prove live/replay state equivalence."""
    payload = asdict(state)
    payload["seen_event_ids"] = sorted(state.seen_event_ids)
    payload["first_entry_proposed_on"] = (
        state.first_entry_proposed_on.isoformat() if state.first_entry_proposed_on else None
    )
    positions: dict[str, dict[str, object]] = {}
    for key in sorted(state.positions):
        position = state.positions[key]
        positions[key] = {
            "contract_key": position.contract_key,
            "status": position.status.value,
            "generation": position.generation,
            "source_entry_message_id": position.source_entry_message_id,
            "source_channel_id": position.source_channel_id,
            "source_author_id": position.source_author_id,
            "last_source_ts_utc": (
                position.last_source_ts_utc.isoformat() if position.last_source_ts_utc else None
            ),
            "quantity": position.quantity,
            "average_price": str(position.average_price),
            "added_once": position.added_once,
            "last_reason": position.last_reason,
        }
    payload["positions"] = positions
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def realized_cost_basis_after_reduction(
    *,
    average_price: Decimal,
    starting_quantity: int,
    sold_quantity: int,
) -> tuple[int, Decimal]:
    if not 0 <= sold_quantity <= starting_quantity:
        raise ValueError("invalid sold quantity")
    remaining = starting_quantity - sold_quantity
    return remaining, average_price if remaining else Decimal("0")

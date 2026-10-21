"""Realised P&L per fill, so the risk layer has something true to evaluate.

`reducer.apply_fill` moves quantity and average cost, which is all a book needs to *hold*
a position. It never computes what a reduction actually earned, so nothing downstream can
know whether the desk is up or down - which is why no risk rule could exist in this
control plane. This module closes that: one settlement per fill, with the arithmetic
stated in the type rather than implied.

Definitions, made explicit because they are where backtests quietly go wrong:

* **cost basis** of the contracts being reduced is `average_price * quantity * 100`;
* **proceeds** are `fill_price * quantity * 100`;
* **realised** is `proceeds - cost basis`, so a loss is negative and a scratch is exactly
  zero;
* an **add** realises nothing - it changes the average, not the result;
* a **final** fill closes whatever quantity remains, so it cannot leave dust behind.

Options carry a 100-share multiplier, applied here once. Nothing in this module rounds a
price: a fill is reported at the price the venue gave.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .domain import BookState, PositionStatus
from .reducer import apply_fill
from .sizing import CONTRACT_MULTIPLIER


class SettlementError(ValueError):
    """A fill that cannot be applied to this book. Never guessed at."""


@dataclass(frozen=True, slots=True)
class Settlement:
    state: BookState
    realized: Decimal
    proceeds: Decimal
    cost_basis: Decimal
    quantity: int
    closed: bool
    reason: str

    @property
    def is_loss(self) -> bool:
        return self.realized < 0


def settle_fill(
    *,
    state: BookState,
    contract_key: str,
    generation: int,
    quantity_delta: int,
    fill_price: Decimal,
    final: bool = False,
) -> Settlement:
    """Apply one fill and report what it realised.

    Raises instead of clamping when a fill is impossible - selling more than is held, a
    non-positive price, a generation mismatch - because a book that silently absorbs an
    impossible fill is worse than a crash: the crash pages someone.
    """
    if fill_price <= 0:
        raise SettlementError(f"fill_price must be positive, got {fill_price}")
    position = state.positions.get(contract_key)
    if position is None:
        raise SettlementError(f"no position for {contract_key}")
    if generation != position.generation:
        raise SettlementError(
            f"generation mismatch for {contract_key}: fill {generation} != book "
            f"{position.generation}"
        )
    if position.status in {PositionStatus.CLOSED, PositionStatus.FLAT}:
        raise SettlementError(f"{contract_key} is already closed")

    held = position.quantity
    average = position.average_price

    if final:
        if held <= 0:
            raise SettlementError(f"final fill with no quantity held for {contract_key}")
        quantity = held
        realized = (fill_price - average) * quantity * CONTRACT_MULTIPLIER
        new_state = apply_fill(
            state,
            contract_key=contract_key,
            generation=generation,
            quantity_delta=0,
            fill_price=fill_price,
            final=True,
        )
        return Settlement(
            state=new_state,
            realized=realized,
            proceeds=fill_price * quantity * CONTRACT_MULTIPLIER,
            cost_basis=average * quantity * CONTRACT_MULTIPLIER,
            quantity=quantity,
            closed=True,
            reason="final_fill",
        )

    if quantity_delta == 0:
        return Settlement(
            state=state,
            realized=Decimal("0"),
            proceeds=Decimal("0"),
            cost_basis=Decimal("0"),
            quantity=0,
            closed=False,
            reason="no_op_fill",
        )

    if quantity_delta > 0:
        new_state = apply_fill(
            state,
            contract_key=contract_key,
            generation=generation,
            quantity_delta=quantity_delta,
            fill_price=fill_price,
        )
        return Settlement(
            state=new_state,
            realized=Decimal("0"),
            proceeds=fill_price * quantity_delta * CONTRACT_MULTIPLIER,
            cost_basis=fill_price * quantity_delta * CONTRACT_MULTIPLIER,
            quantity=quantity_delta,
            closed=False,
            reason="add_fill",
        )

    quantity = -quantity_delta
    if quantity > held:
        raise SettlementError(
            f"cannot reduce {contract_key} by {quantity}: only {held} held"
        )
    realized = (fill_price - average) * quantity * CONTRACT_MULTIPLIER
    new_state = apply_fill(
        state,
        contract_key=contract_key,
        generation=generation,
        quantity_delta=quantity_delta,
        fill_price=fill_price,
    )
    closed = new_state.positions[contract_key].status is PositionStatus.CLOSED
    return Settlement(
        state=new_state,
        realized=realized,
        proceeds=fill_price * quantity * CONTRACT_MULTIPLIER,
        cost_basis=average * quantity * CONTRACT_MULTIPLIER,
        quantity=quantity,
        closed=closed,
        reason="reduction_fill",
    )

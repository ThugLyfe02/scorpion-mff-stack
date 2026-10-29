"""Fills: partial executions as a first-class fact, not an edge case.

An order for five contracts that fills three and then two is not one event, and every downstream
number changes when it is treated as one: average cost, realised P&L, whether the position is still
open, whether a scale-out has anything left to sell, and whether the desk thinks it is flat when it
is not.

This module owns that. A fill is applied incrementally, a position tracks what is left of each
intended clip, and the invariants are stated as tests:

* partial fills compose: three then two realises exactly what one fill of five would;
* a fill of two against a five-lot intent leaves three outstanding, not zero;
* an overfill is refused rather than silently clamped, because a venue telling you it filled more
  than you asked for is a reconciliation event, not a rounding problem;
* a fill after the position is closed is refused, because that is a bookkeeping error somewhere
  upstream and swallowing it hides the error.

Design note on the ledger's honesty: `qty_filled` and `qty_intended` are separate. Collapsing them
into one number is how a system concludes it is flat when it holds a residual, and a residual option
position costs money in a way a rounding error never does.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

D = Decimal


class FillError(ValueError):
    """A fill that cannot be applied, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution report from a venue, however partial."""

    contract_key: str
    generation: int
    quantity: int                    # signed: positive buys, negative sells
    price: D
    intent_quantity: int = 0         # what the order asked for, for fill-ratio accounting
    venue_ref: str = ""
    final: bool = False              # the venue says no more will arrive

    def __post_init__(self) -> None:
        if self.quantity == 0 and not self.final:
            raise FillError("zero_quantity", "a fill with no quantity is not a fill")
        if self.price < 0:
            raise FillError("negative_price", str(self.price))
        if not self.contract_key:
            raise FillError("no_contract", "a fill must name its contract")

    @property
    def side(self) -> str:
        return "BUY" if self.quantity > 0 else "SELL"

    @property
    def fill_ratio(self) -> D | None:
        if self.intent_quantity <= 0:
            return None
        return (D(abs(self.quantity)) / D(self.intent_quantity)).quantize(D("0.0001"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_key": self.contract_key, "generation": self.generation,
            "quantity": self.quantity, "price": str(self.price),
            "intent_quantity": self.intent_quantity, "side": self.side,
            "venue_ref": self.venue_ref, "final": self.final,
        }


@dataclass(frozen=True, slots=True)
class Position:
    """What is actually held, and what is still outstanding on an intent."""

    contract_key: str
    generation: int
    quantity: int = 0
    outstanding: int = 0             # contracts still to arrive on an open intent
    cost: D = D("0")
    fills: tuple[Fill, ...] = ()
    closed: bool = False

    @property
    def flat(self) -> bool:
        return self.quantity == 0 and self.outstanding == 0

    @property
    def average_price(self) -> D:
        filled = sum(abs(f.quantity) for f in self.fills if f.quantity > 0)
        if not filled:
            return D("0")
        # `cost` is dollar notional (premium x contracts x 100), so the per-contract price is
        # cost divided by contracts AND by the multiplier - not by contracts alone.
        return (self.cost / (D(filled) * D(100))).quantize(D("0.0001"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_key": self.contract_key, "generation": self.generation,
            "quantity": self.quantity, "outstanding": self.outstanding,
            "average_price": str(self.average_price), "closed": self.closed,
            "fills": [f.to_dict() for f in self.fills],
        }


@dataclass(frozen=True, slots=True)
class ApplyResult:
    position: Position
    realised: D
    filled_now: int
    remaining: int
    complete: bool


def open_position(contract_key: str, generation: int, *, intent_quantity: int = 0) -> Position:
    return Position(contract_key=contract_key, generation=generation,
                    outstanding=max(0, intent_quantity))


def apply(position: Position, fill: Fill) -> ApplyResult:
    """Apply one (possibly partial) fill. Never clamps, never guesses."""
    if fill.contract_key != position.contract_key:
        raise FillError("wrong_contract",
                        f"{fill.contract_key} cannot settle {position.contract_key}")
    if fill.generation != position.generation:
        raise FillError("stale_generation",
                        f"fill generation {fill.generation} vs position {position.generation}")
    if position.closed and fill.quantity != 0:
        raise FillError("position_closed", f"{position.contract_key} is closed")

    buying = fill.quantity > 0
    quantity = position.quantity
    cost = position.cost
    realised = D("0")
    outstanding = position.outstanding

    if buying:
        if outstanding and abs(fill.quantity) > outstanding:
            raise FillError(
                "overfill",
                f"asked {outstanding}, filled {abs(fill.quantity)} - reconciliation event",
            )
        quantity += fill.quantity
        cost += fill.price * D(fill.quantity) * D(100)
        outstanding = max(0, outstanding - fill.quantity)
    else:
        selling = abs(fill.quantity)
        if selling > quantity:
            raise FillError(
                "oversell", f"holding {quantity}, asked to sell {selling}"
            )
        # realised is computed against the average cost of what is held, not against the last fill
        basis = (cost / (D(quantity) * D(100))).quantize(D("0.0001")) if quantity else D("0")
        realised = ((fill.price - basis) * D(selling) * D(100)).quantize(D("0.01"))
        quantity -= selling
        cost -= basis * D(selling) * D(100)
        if quantity == 0:
            cost = D("0")

    closed = quantity == 0 and (outstanding == 0) and fill.final
    updated = replace(
        position, quantity=quantity, cost=cost, outstanding=outstanding,
        fills=position.fills + (fill,), closed=closed,
    )
    return ApplyResult(
        position=updated, realised=realised, filled_now=abs(fill.quantity),
        remaining=outstanding, complete=updated.flat and closed,
    )


def apply_many(position: Position, fills: Iterable[Fill]) -> tuple[Position, D, list[ApplyResult]]:
    """Apply a sequence, accumulating realised P&L. Stops at the first impossible fill."""
    total = D("0")
    results: list[ApplyResult] = []
    current = position
    for fill in fills:
        outcome = apply(current, fill)
        current = outcome.position
        total += outcome.realised
        results.append(outcome)
    return current, total, results


def fill_report(fills: Sequence[Fill]) -> dict[str, Any]:
    """How completely the venue filled what was asked."""
    if not fills:
        return {"fills": 0, "intended": 0, "filled": 0, "ratio": None, "partial_count": 0}
    intended = max((f.intent_quantity for f in fills), default=0)
    filled = sum(abs(f.quantity) for f in fills)
    partials = sum(1 for f in fills if f.intent_quantity and abs(f.quantity) < f.intent_quantity)
    return {
        "fills": len(fills),
        "intended": intended,
        "filled": filled,
        "ratio": round(filled / intended, 4) if intended else None,
        "partial_count": partials,
        "average_price": str(
            (sum((f.price * D(abs(f.quantity)) for f in fills), D("0")) / D(filled or 1))
            .quantize(D("0.0001"))
        ),
    }


def equivalence_check(
    intent: int, chunks: Sequence[int], price: D, exit_price: D
) -> dict[str, Any]:
    """A split fill and a single fill must realise the same money. Used as a test and as a guard."""
    single = Position(contract_key="X", generation=1, quantity=intent,
                      cost=price * D(intent) * D(100), fills=())
    single_result = apply(single, Fill("X", 1, -intent, exit_price, final=True))

    split_position = open_position("X", 1, intent_quantity=intent)
    running = split_position
    for chunk in chunks:
        running = apply(running, Fill("X", 1, chunk, price, intent_quantity=intent)).position
    split_result = apply(running, Fill("X", 1, -intent, exit_price, final=True))

    return {
        "identical": single_result.realised == split_result.realised,
        "single_realised": str(single_result.realised),
        "split_realised": str(split_result.realised),
        "delta": str(single_result.realised - split_result.realised),
    }

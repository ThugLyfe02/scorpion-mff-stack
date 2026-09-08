"""Correctness primitives for replacing the legacy 5-minute-bar backtest.

This module intentionally does not reproduce the old "Discord final % => synthetic target ladder"
logic. Historical evaluation should replay source messages chronologically and use these primitives
to treat entry-bar OHLC ordering as unknown.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Lot:
    quantity: int
    average: Decimal

    def buy(self, quantity: int, price: Decimal) -> "Lot":
        if quantity <= 0 or price <= 0:
            raise ValueError("invalid buy")
        total_qty = self.quantity + quantity
        total_cost = self.average * self.quantity + price * quantity
        return Lot(total_qty, total_cost / total_qty)

    def sell(self, quantity: int) -> "Lot":
        if quantity <= 0 or quantity > self.quantity:
            raise ValueError("invalid sell")
        remaining = self.quantity - quantity
        return Lot(remaining, self.average if remaining else Decimal("0"))


@dataclass(frozen=True, slots=True)
class FiveMinuteBar:
    begins_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def target_bounds_on_entry_bar(
    *,
    entry_price: Decimal,
    target_price: Decimal,
    bar: FiveMinuteBar,
) -> tuple[bool, bool]:
    """Return (optimistic_hit, conservative_hit).

    With only OHLC, a target printed somewhere in the same bar as the inferred entry cannot be
    known to have printed *after* entry. Conservative replay therefore does not credit it.
    """
    if target_price >= entry_price:
        printed = bar.high >= target_price
    else:
        printed = bar.low <= target_price
    return printed, False if printed else False


def position_is_open_at(exit_ts: datetime, candidate_entry_ts: datetime) -> bool:
    return exit_ts > candidate_entry_ts

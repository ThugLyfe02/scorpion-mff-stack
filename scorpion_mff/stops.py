"""Mark-to-market, and the breakeven stop the engine recorded but never enforced.

The lock says a trim leaves "1 contract + breakeven stop". The engine sets
`Position.breakeven_stop = avg_price` and writes `stop->be@...` into the fill log,
but nothing ever reads that field back: a later quote below breakeven does nothing,
and the stop is a note rather than a fact.

This module closes that seam in two directions:

  * mark the open book to a live bid - unrealised P&L, at/through-cost flags, and
    a total that is **withheld** when any position cannot be priced, so a partial
    view never masquerades as a full one;
  * emit a flatten intent when a bid trades at or through a position's breakeven
    stop, so "trim to 1 + BE" is a property of the book, not a comment in the log.

Both are pure functions of (book, quote). A replay can therefore reproduce every
stop and every mark, and the engine consults them without a second source of truth
about the book.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import Fill, OrderIntent, Position, Side


def unrealized_usd(position: Position, bid: float) -> float:
    """Unrealised P&L in dollars for one position marked to `bid`."""
    return (bid - position.avg_price) * position.contracts * 100.0


@dataclass(frozen=True)
class Mark:
    contract_key: str
    contracts: int
    avg_price: float
    bid: float
    unrealized_usd: float
    breakeven_stop: float | None
    stopped: bool

    @property
    def at_or_below_cost(self) -> bool:
        return self.bid <= self.avg_price


@dataclass(frozen=True)
class Marks:
    """The book marked to market. `total_unrealized_usd` is None when incomplete."""

    marks: tuple[Mark, ...]
    priced: int
    unpriced: int

    @property
    def total_unrealized_usd(self) -> float | None:
        """Withheld when any open position is unpriced: a partial total lies."""
        if self.unpriced > 0:
            return None
        return round(sum(m.unrealized_usd for m in self.marks), 2)

    @property
    def stopped_count(self) -> int:
        return sum(1 for m in self.marks if m.stopped)


def mark_position(position: Position, bid: float) -> Mark:
    return Mark(
        contract_key=position.contract_key,
        contracts=position.contracts,
        avg_price=position.avg_price,
        bid=bid,
        unrealized_usd=round(unrealized_usd(position, bid), 2),
        breakeven_stop=position.breakeven_stop,
        stopped=(position.breakeven_stop is not None and bid <= position.breakeven_stop),
    )


def mark_book(positions: Iterable[Position], bids: Mapping[str, float]) -> Marks:
    """Mark every open position to its bid; leave (and count) the unpriced ones."""
    marks: list[Mark] = []
    unpriced = 0
    for position in positions:
        if not position.is_open:
            continue
        bid = bids.get(position.contract_key)
        if bid is None:
            unpriced += 1
            continue
        marks.append(mark_position(position, bid))
    return Marks(marks=tuple(marks), priced=len(marks), unpriced=unpriced)


def breakeven_exit(position: Position, bid: float) -> OrderIntent | None:
    """A flatten intent when `bid` has traded at or through the breakeven stop.

    None when the position has no stop armed, is flat, or is still above the stop.
    The fill price is the bid actually seen - never the stop price - so realised
    P&L stays honest when the market gaps through the stop.
    """
    if not position.is_open or position.breakeven_stop is None:
        return None
    if bid > position.breakeven_stop:
        return None
    return OrderIntent(
        action="sell",
        qty=position.contracts,
        limit=bid,
        contract_key=position.contract_key,
        reason=f"breakeven_stop:bid={bid:g}<=be={position.breakeven_stop:g}",
        idempotency_key=f"{position.contract_key}:stop",
    )


def breakeven_triggers(
    positions: Iterable[Position], bids: Mapping[str, float]
) -> list[OrderIntent]:
    """All stop intents the current book demands, for a batch quote refresh."""
    intents: list[OrderIntent] = []
    for position in positions:
        if not position.is_open:
            continue
        bid = bids.get(position.contract_key)
        if bid is None:
            continue
        intent = breakeven_exit(position, bid)
        if intent is not None:
            intents.append(intent)
    return intents


def book_from_fills(fills: Iterable[Fill]) -> dict[str, Position]:
    """Rebuild the open book from the append-only fill log, stops included.

    `exposure.positions_from_fills` reconstructs contracts and average price but
    drops the breakeven stop, because the stop is not a structured field - it is
    carried in the trim's note (`stop->be@...`). This reconstruction keeps it, so
    `marks` can flag the exact position the engine would stop on the next quote.
    """
    book: dict[str, Position] = {}
    for fill in sorted(fills, key=lambda item: item.seq):
        if not fill.contract_key:
            continue
        parts = fill.contract_key.split("|")
        if len(parts) < 2 or parts[1] not in {"CALL", "PUT"}:
            continue

        if fill.event == "buy" and fill.accepted:
            position = book.get(fill.contract_key)
            if position is None:
                position = Position(
                    contract_key=fill.contract_key,
                    ticker=parts[0],
                    side=Side(parts[1]),
                    contracts=0,
                    avg_price=0.0,
                    opened_ts=fill.ts,
                    channel=fill.note or "",
                )
                book[fill.contract_key] = position
            total = position.contracts + fill.qty
            if total > 0:
                position.avg_price = (
                    position.avg_price * position.contracts + fill.price * fill.qty
                ) / total
            position.contracts = total
        elif fill.event in {"sell", "expire"}:
            position = book.get(fill.contract_key)
            if position is None:
                continue
            if fill.event == "sell" and fill.note.startswith("stop->be@"):
                # A trim arms the breakeven stop at the cost basis the engine held
                # when it trimmed. The engine records avg_price at that moment, and
                # this note carries the marker rather than the number.
                position.breakeven_stop = position.avg_price
            position.contracts = max(0, position.contracts - fill.qty)
            if position.contracts == 0:
                del book[fill.contract_key]

    return book


def load_bids(path: str | Path) -> dict[str, float]:
    """Read a quotes file into `contract_key -> bid`.

    Accepts the tape shape (`{"quotes": {key: [bid, ask], ...}}`) and a plain
    map (`{key: [bid, ask], ...}` or `{key: {"bid": b, "ask": a}, ...}`). A row
    that names neither shape is skipped, so a mixed file never invents a bid.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and set(payload) == {"quotes"} and isinstance(
        payload["quotes"], dict
    ):
        payload = payload["quotes"]

    bids: dict[str, float] = {}
    for key, row in payload.items():
        if isinstance(row, dict):
            value: Any = row.get("bid")
        elif isinstance(row, (list, tuple)) and len(row) >= 1:
            value = row[0]
        else:
            continue
        try:
            bids[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return bids

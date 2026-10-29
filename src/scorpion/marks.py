"""Mark-to-market the open book, with the withholding the rest of the lane honours.

The control plane can decide what to trade and refuse what it cannot disambiguate, but
it has no "what is the book worth right now" view: `settlement.py` prices realised fills,
`markout.py` prices what a trade did historically, and `tca.py` prices how well we got it.
None of them answers the operator question that matters between events - *if I had to
exit everything at the next quote, what is the unrealised P&L?*

`mark` answers it from the same `BookState` the reducer owns, so it is reproducible from
the applied log and cannot drift from the book. Like every aggregate in this lane, the
total **withholds itself** when the inputs cannot support it: a missing bid for one open
position makes the sum a fiction, so the sum is `None` rather than a number that is
almost right. Individual marks are still shown - a human reading one row at a time knows
exactly which position it is missing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .broker import Quote
from .domain import BookState

CENT = Decimal("0.01")
MULTIPLIER = Decimal(100)          # one option contract = 100 shares


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def unrealized_usd(quantity: int, average_price: Decimal, bid: Decimal) -> Decimal:
    """Unrealised P&L for one position, in dollars, to the cent."""
    return _money((bid - average_price) * Decimal(quantity) * MULTIPLIER)


@dataclass(frozen=True, slots=True)
class Mark:
    contract_key: str
    quantity: int
    average_price: Decimal
    bid: Decimal
    unrealized_usd: Decimal

    @property
    def at_or_below_cost(self) -> bool:
        return self.bid <= self.average_price


@dataclass(frozen=True, slots=True)
class MarkBook:
    marks: tuple[Mark, ...]
    priced: int
    unpriced: int
    stale: int = 0

    @property
    def total_unrealized_usd(self) -> Decimal | None:
        """Withheld (`None`) when any open position is unpriced or stale-priced; else
        summed to the cent.

        A partial total would be presented as "the book's P&L" when it is only the P&L
        of the positions we happened to be able to price. A stale bid is the same
        failure as a missing bid in different clothes - a phantom price - so it
        withholds the sum exactly as a missing bid does. That is the same lie
        `markout` and `tca` refuse to tell about historical coverage, here about the
        live book. A flat book (nothing held) is an honest zero, not a withholding.
        """
        if self.unpriced > 0 or self.stale > 0:
            return None
        return _money(sum((m.unrealized_usd for m in self.marks), start=Decimal("0")))

    def to_dict(self) -> dict[str, object]:
        return {
            "priced": self.priced,
            "unpriced": self.unpriced,
            "stale": self.stale,
            "total_unrealized_usd": None if self.total_unrealized_usd is None
            else str(self.total_unrealized_usd),
            "marks": [
                {
                    "contract_key": m.contract_key,
                    "quantity": m.quantity,
                    "average_price": str(m.average_price),
                    "bid": str(m.bid),
                    "unrealized_usd": str(m.unrealized_usd),
                    "at_or_below_cost": m.at_or_below_cost,
                }
                for m in self.marks
            ],
        }

    def to_text(self) -> str:
        total = self.total_unrealized_usd
        headline = ("WITHHELD (a position cannot be priced)" if total is None
                    else f"${total}")
        lines = [
            f"priced {self.priced} | unpriced {self.unpriced} | stale {self.stale}",
            f"unrealised P&L: {headline}",
        ]
        for m in self.marks:
            lines.append(
                f"  {m.quantity:>3}x {m.contract_key:<34} avg {m.average_price:<8} "
                f"bid {m.bid:<8} unreal ${m.unrealized_usd}"
            )
        return "\n".join(lines)


def mark(book: BookState, bids: Mapping[str, Decimal], *, stale: int = 0) -> MarkBook:
    """Mark every held position to its bid. Missing bids are counted, never invented.

    `stale` carries the count of quotes dropped for age by the caller: a stale bid
    is a phantom price, so it must count against the total exactly as a missing bid
    does, and the caller is the one that knows which quotes were dropped.
    """
    marks: list[Mark] = []
    unpriced = 0
    for position in book.positions.values():
        if position.quantity <= 0:
            continue
        bid = bids.get(position.contract_key)
        if bid is None:
            unpriced += 1
            continue
        marks.append(Mark(
            contract_key=position.contract_key,
            quantity=position.quantity,
            average_price=position.average_price,
            bid=bid,
            unrealized_usd=unrealized_usd(position.quantity, position.average_price, bid),
        ))
    return MarkBook(marks=tuple(marks), priced=len(marks), unpriced=unpriced, stale=stale)


def split_stale(
    quotes: Mapping[str, Quote], now: datetime, max_age: timedelta
) -> tuple[dict[str, Quote], int]:
    """Partition quotes into fresh vs stale.

    A stale bid must not price a position: it is a phantom price, and marking to it
    produces a phantom P&L. The stale count is returned so the caller can withhold
    the total, exactly as it withholds for a missing bid. A quote exactly `max_age`
    old is still fresh (the ceiling is exclusive, matching the locked +25% gate's
    strict-boundary convention).
    """
    fresh: dict[str, Quote] = {}
    stale = 0
    for key, quote in quotes.items():
        if now - quote.observed_ts_utc > max_age:
            stale += 1
        else:
            fresh[key] = quote
    return fresh, stale

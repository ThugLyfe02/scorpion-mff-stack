"""Get flat, on purpose: sell every confirmed lot at a bid that exists.

Why this module exists
----------------------
Every exit in this stack was *event-shaped*: a Discord post, a breakeven quote, or the
15:45 cutoff. There was no way for a human to say "I want to be flat now" - and the
moments that command is for (the channel is compromised, a position is mis-sized, the
world changed) are exactly the moments a post is not coming. `killswitch` stops new buys;
it deliberately does not, and must not, trap a position.

Three rules make this safe enough to be the panic button:

* **A price we can see, or nothing.** The sell limit is the live bid, never the entry, the
  last bar, the posted price, or a market order. A lot whose quote cannot be read is
  reported as `UNPRICED` and left on the book. Inventing a bid is not flattening.
* **One exit per lot.** A contract with an unanswered sell at the venue is `IN_FLIGHT`
  and is never sold again. That is the guard that stops a panic from becoming a short.
* **The confirmed book is the list.** The command sells what the venue confirmed, not what
  the engine decided. A position the book does not have is not a position this command can
  accidentally create.

Two modes: `plan` (default) prints what it *would* do and touches nothing; `execute`
places each sell through the live listener, so the intent row, the venue answer, the
confirmed book, the realised-P&L correction and the in-flight reconciliation are all the
same code an exit post uses. The kill switch is not consulted: it stops entries, and a
desk that cannot flatten because it stopped buying is a desk with the safety inverted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .execution_book import FILLED, PARTIAL, ExecutionBook
from .types import AlertKind, Position, Quote, Side, Signal

if TYPE_CHECKING:                       # the listener imports this module's caller
    from .broker import QuoteProvider
    from .engine import Engine
    from .listener import Listener

OPTION_MULTIPLIER = 100.0

PLANNED = "planned"
PLACED = "placed"
IN_FLIGHT = "in_flight"
UNPRICED = "unpriced"
FAILED = "failed"
PARTIAL_FILL = "partial"
EXPIRED = "expired"


@dataclass(frozen=True)
class FlattenLine:
    """One confirmed lot and what the command can do about it."""

    contract_key: str
    qty: int
    avg_price: float
    bid: float | None
    estimated_pnl_usd: float | None
    status: str
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_key": self.contract_key, "qty": self.qty,
            "avg_price": round(self.avg_price, 4),
            "bid": None if self.bid is None else round(self.bid, 4),
            "estimated_pnl_usd": (None if self.estimated_pnl_usd is None
                                  else round(self.estimated_pnl_usd, 2)),
            "status": self.status, "note": self.note,
        }


@dataclass
class FlattenReport:
    """What happened, and whether the book is flat at the end."""

    mode: str                                   # "plan" | "execute"
    lines: list[FlattenLine] = field(default_factory=list)
    expired: int = 0
    remaining_contracts: int = 0
    remaining_positions: int = 0
    page: str = ""                              # anything a human must read

    @property
    def flat(self) -> bool:
        return self.remaining_contracts == 0

    @property
    def exit_code(self) -> int:
        if self.mode == "plan":
            return 0 if all(line.status in (PLANNED, IN_FLIGHT)
                            for line in self.lines) else 1
        return 0 if self.flat else 1

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode, "flat": self.flat, "exit_code": self.exit_code,
            "expired": self.expired,
            "remaining_contracts": self.remaining_contracts,
            "remaining_positions": self.remaining_positions,
            "lines": [line.to_dict() for line in self.lines],
            "page": self.page,
        }

    def to_text(self) -> str:
        verb = "PLAN" if self.mode == "plan" else "LIVE"
        if not self.lines and not self.expired:
            return f"flatten ({verb}) | nothing to flatten: the confirmed book is flat"
        counts: dict[str, int] = {}
        for line in self.lines:
            counts[line.status] = counts.get(line.status, 0) + 1
        summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
        lines = [f"flatten ({verb}) | {len(self.lines)} lot(s)"
                 + (f" | {summary}" if summary else "")
                 + (f" | {self.expired} expired" if self.expired else "")]
        for line in self.lines:
            bid = "-" if line.bid is None else f"{line.bid:.2f}"
            pnl = ("-" if line.estimated_pnl_usd is None
                   else f"{line.estimated_pnl_usd:+,.2f}")
            lines.append(f"  {line.status.upper():<9}{line.contract_key:<30} "
                         f"{line.qty:>3} @ bid {bid:<7} avg {line.avg_price:.2f}  "
                         f"est {pnl}"
                         + (f"  {line.note}" if line.note else ""))
        if self.mode == "plan":
            lines.append("  dry run: nothing was sent. Re-run with --live to flatten.")
        lines.append(f"  book after: {self.remaining_positions} position(s) / "
                     f"{self.remaining_contracts} contract(s) still open")
        if self.page:
            lines.append(f"  PAGE: {self.page}")
        return "\n".join(lines)


def _in_flight_sells(book: ExecutionBook) -> set[str]:
    """Contracts with a sell the venue has not finished answering."""
    return {order.contract_key for order in book.unresolved_orders()
            if order.action == "sell"}


def _bid_from(quotes: QuoteProvider, contract_key: str) -> Quote | None:
    """A quote, or None. A failing feed is silence, never a price."""
    try:
        quote = quotes.quote(contract_key)
    except Exception:            # noqa: BLE001 - unpriceable is not priced
        return None
    if quote is None:
        return None
    if not math.isfinite(quote.bid) or quote.bid <= 0:
        return None
    return quote


def _signal_for(position: Position, bid: float) -> Signal | None:
    """Rebuild the close signal the live path would have received.

    The lot's contract key is the identity, so the signal is built from it rather than
    from the ticker (two calls on one ticker are two different lots). A key this parser
    cannot read is reported, not guessed at.
    """
    parts = position.contract_key.split("|")
    if len(parts) < 3:
        return None
    try:
        side = Side(parts[1].strip().upper())
        strike = float(parts[2])
    except (ValueError, KeyError):
        return None
    expiry = parts[3] if len(parts) > 3 else ""
    signal = Signal(kind=AlertKind.CLOSE, channel=position.channel, ticker=parts[0],
                    side=side, strike=strike, expiry=expiry, price=bid,
                    raw=f"flatten:{position.contract_key}")
    if signal.contract_key != position.contract_key:
        return None
    return signal


def plan(book: ExecutionBook, quotes: QuoteProvider) -> FlattenReport:
    """What a flatten would sell, at what bid, with no side effect at all."""
    report = FlattenReport(mode="plan")
    in_flight = _in_flight_sells(book)
    for position in sorted(book.open_positions(), key=lambda p: p.contract_key):
        if position.contract_key in in_flight:
            report.lines.append(FlattenLine(
                contract_key=position.contract_key, qty=position.contracts,
                avg_price=position.avg_price, bid=None, estimated_pnl_usd=None,
                status=IN_FLIGHT, note="a sell is already at the venue"))
            continue
        quote = _bid_from(quotes, position.contract_key)
        if quote is None:
            report.lines.append(FlattenLine(
                contract_key=position.contract_key, qty=position.contracts,
                avg_price=position.avg_price, bid=None, estimated_pnl_usd=None,
                status=UNPRICED, note="no live bid; nothing would be sent"))
            continue
        estimate = (quote.bid - position.avg_price) * position.contracts * OPTION_MULTIPLIER
        report.lines.append(FlattenLine(
            contract_key=position.contract_key, qty=position.contracts,
            avg_price=position.avg_price, bid=quote.bid,
            estimated_pnl_usd=estimate, status=PLANNED))
    report.remaining_positions = len(book.open_positions())
    report.remaining_contracts = sum(p.contracts for p in book.open_positions())
    return report


def execute(book: ExecutionBook, engine: Engine, listener: Listener,
            quotes: QuoteProvider, now: float) -> FlattenReport:
    """Sell every confirmed lot through the live ladder.

    `sync_engine` first: a lot whose contract already expired is released (it cannot be
    sold), and a lot carried from a previous session is restored before it is priced.
    Then each remaining lot gets the signal a close post would produce, priced at the
    bid actually seen, and is placed by the listener - so the intent, the venue answer,
    the confirmed book and the in-flight guard are the same ones an exit post uses.
    """
    report = FlattenReport(mode="execute")
    sync = book.sync_engine(engine, now)
    report.expired = sync.expired
    in_flight = _in_flight_sells(book)
    for position in sorted(engine.open_positions, key=lambda p: p.contract_key):
        if position.contract_key in in_flight or position.contract_key in engine.exit_in_flight:
            report.lines.append(FlattenLine(
                contract_key=position.contract_key, qty=position.contracts,
                avg_price=position.avg_price, bid=None, estimated_pnl_usd=None,
                status=IN_FLIGHT, note="a sell is already at the venue"))
            continue
        quote = _bid_from(quotes, position.contract_key)
        if quote is None:
            report.lines.append(FlattenLine(
                contract_key=position.contract_key, qty=position.contracts,
                avg_price=position.avg_price, bid=None, estimated_pnl_usd=None,
                status=UNPRICED, note="no live bid; nothing sent"))
            report.page = (report.page + " " if report.page else "") + \
                f"unpriced:{position.contract_key}"
            continue
        signal = _signal_for(position, quote.bid)
        if signal is None:
            report.lines.append(FlattenLine(
                contract_key=position.contract_key, qty=position.contracts,
                avg_price=position.avg_price, bid=quote.bid, estimated_pnl_usd=None,
                status=FAILED, note="contract key cannot become a close signal"))
            continue
        intents = engine.on_signal(signal, quote=quote, now=now)
        if not intents:
            last = engine.fills.records[-1] if engine.fills.records else None
            report.lines.append(FlattenLine(
                contract_key=position.contract_key, qty=position.contracts,
                avg_price=position.avg_price, bid=quote.bid,
                estimated_pnl_usd=(quote.bid - position.avg_price)
                                  * position.contracts * OPTION_MULTIPLIER,
                status=FAILED, note=f"engine refused: {last.reason if last else 'no intent'}"))
            continue
        for intent in intents:
            listener.place(intent)
        order = next((candidate for candidate in book.orders.values()
                      if candidate.intent.idempotency_key == intents[0].idempotency_key),
                     None)
        estimate = (quote.bid - position.avg_price) * position.contracts * OPTION_MULTIPLIER
        if order is None:
            report.lines.append(FlattenLine(
                contract_key=position.contract_key, qty=position.contracts,
                avg_price=position.avg_price, bid=quote.bid,
                estimated_pnl_usd=estimate, status=FAILED,
                note="the confirmed book has no row for the send"))
            report.page = (report.page + " " if report.page else "") + \
                f"book_write:{position.contract_key}"
            continue
        if order.state == FILLED:
            report.lines.append(FlattenLine(
                contract_key=position.contract_key, qty=position.contracts,
                avg_price=position.avg_price, bid=order.avg_price or quote.bid,
                estimated_pnl_usd=(order.avg_price - position.avg_price)
                                  * position.contracts * OPTION_MULTIPLIER,
                status=PLACED))
        elif order.state == PARTIAL:
            report.lines.append(FlattenLine(
                contract_key=position.contract_key, qty=order.filled_qty,
                avg_price=position.avg_price, bid=order.avg_price or quote.bid,
                estimated_pnl_usd=(order.avg_price - position.avg_price)
                                  * order.filled_qty * OPTION_MULTIPLIER,
                status=PARTIAL_FILL,
                note=f"{order.remaining} left; an exit is in flight"))
            report.page = (report.page + " " if report.page else "") + \
                f"partial:{position.contract_key}:{order.remaining}_left"
        else:
            report.lines.append(FlattenLine(
                contract_key=position.contract_key, qty=position.contracts,
                avg_price=position.avg_price, bid=quote.bid,
                estimated_pnl_usd=None, status=FAILED,
                note=order.error or order.last_unknown or order.state.lower()))
            report.page = (report.page + " " if report.page else "") + \
                f"exit_failed:{position.contract_key}:{order.state.lower()}"
    open_positions = book.open_positions()
    report.remaining_positions = len(open_positions)
    report.remaining_contracts = sum(p.contracts for p in open_positions)
    return report

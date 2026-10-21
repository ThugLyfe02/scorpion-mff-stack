"""A deterministic paper venue, so the whole loop can be exercised without credentials.

`PaperBroker` in `broker.py` is a synchronous adapter that fills at a price it is handed.
This is the other half: a `Sender` for the dispatcher, which means the durable outbox,
the authorization ladder, the settlement arithmetic and TCA can all be driven end to end
today, with no venue, no token, and no network.

Two properties are deliberate:

* **deterministic** - the same scenario produces the same fills, every run, so a failure is
  reproducible rather than "sometimes";
* **scriptable** - `script` lets a test inject a timeout, a rejection, or a partial fill at a
  chosen point in the sequence, which is how the retry and dead-letter paths get exercised
  against the real dispatcher rather than against a mock of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .authorization import OrderEnvelope
from .broker import Quote
from .outbox import Receipt


@dataclass(frozen=True, slots=True)
class PaperConfig:
    """How the pretend venue behaves. Zero by default: a marketable limit fills at its limit."""

    slippage_bps: Decimal = Decimal("0")
    partial_ratio: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must not be negative")
        if not (Decimal("0") < self.partial_ratio <= Decimal("1")):
            raise ValueError("partial_ratio must be in (0, 1]")


DEFAULT_PAPER = PaperConfig()


@dataclass(slots=True)
class PaperSender:
    """A `Sender` that fills at the limit, with optional deterministic slippage.

    `script` overrides the default behaviour for the next N sends: put a `Receipt` to return
    it verbatim, or an exception to make the sender raise. Consumed in order.
    """

    config: PaperConfig = field(default_factory=PaperConfig)
    script: list[Receipt | Exception] = field(default_factory=list)
    seen: list[OrderEnvelope] = field(default_factory=list)
    fills: list[Receipt] = field(default_factory=list)
    _counter: int = field(default=0)

    def send(self, envelope: OrderEnvelope, *, now: datetime) -> Receipt:
        self.seen.append(envelope)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            self.fills.append(item)
            return item

        self._counter += 1
        slip = self.config.slippage_bps / Decimal("10000")
        if envelope.side == "BUY":
            price = envelope.limit_price * (Decimal("1") + slip)
        else:
            price = envelope.limit_price * (Decimal("1") - slip)
        quantity = int(Decimal(envelope.quantity) * self.config.partial_ratio) or envelope.quantity
        receipt = Receipt(
            ok=True,
            broker_ref=f"paper-{self._counter}",
            fill_price=round(max(Decimal("0.01"), price), 4),
            fill_qty=quantity,
        )
        self.fills.append(receipt)
        return receipt


def quote(bid: str, ask: str, *, at: datetime, age_ms: int = 0) -> Quote:
    """A quote stamped `age_ms` before `at` - fresh enough to authorize by default."""
    return Quote(
        bid=Decimal(bid),
        ask=Decimal(ask),
        observed_ts_utc=at - timedelta(milliseconds=age_ms),
    )


def now() -> datetime:
    return datetime.now(UTC)

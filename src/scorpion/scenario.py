"""The vertical slice: one scenario drives every layer and reports what happened.

This exists because a system of well-tested parts can still be broken as a whole. The
scenario runs the real pipeline - parse, associate, reduce (with risk), authorize, dispatch
through the durable outbox, settle, and measure - and returns a trail that says, for every
message, what each layer decided. When the live credentials arrive, the same runner is the
shadow harness: swap `PaperSender` for a real `Sender` and the trail becomes an execution
record.

Nothing here mocks the pipeline. `PaperSender` is a real implementation of the `Sender`
protocol, and the store is a real SQLite database, so the test of this module is a test of
the production wiring.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from .broker import Quote
from .config import CHANNELS, GUILD_ID, Policy
from .domain import RawDiscordMessage
from .outbox import Dispatched, Dispatcher, DispatchReport, Receipt
from .paper import PaperSender
from .pipeline import Pipeline
from .store import Store
from .tca import FillReport, report_from_store

CHANNEL_ID = CHANNELS["etf-options"]
QuoteSource = Mapping[str, Quote] | Callable[[str, datetime], Quote]
GUILD = GUILD_ID
AUTHOR = "signal-author"


@dataclass(frozen=True, slots=True)
class Step:
    label: str
    detail: str

    def __str__(self) -> str:
        return f"{self.label:<22} {self.detail}"


@dataclass
class ScenarioResult:
    steps: list[Step] = field(default_factory=list)
    dispatches: list[DispatchReport] = field(default_factory=list)
    settlements: list[str] = field(default_factory=list)
    tca: FillReport = field(default_factory=FillReport)
    halted: bool = False
    open_positions: int = 0
    realized_usd: Decimal = Decimal("0")
    risk_reasons: tuple[str, ...] = ()
    verified: bool = False
    verification: dict[str, object] = field(default_factory=dict)

    @property
    def sent(self) -> int:
        return sum(r.sent for r in self.dispatches)

    @property
    def refused(self) -> int:
        return sum(r.refused for r in self.dispatches)

    def to_dict(self) -> dict[str, object]:
        return {
            "steps": [{"label": s.label, "detail": s.detail} for s in self.steps],
            "sent": self.sent,
            "refused": self.refused,
            "realized_usd": str(self.realized_usd),
            "open_positions": self.open_positions,
            "halted": self.halted,
            "risk_reasons": list(self.risk_reasons),
            "tca": self.tca.to_dict(),
            "dispatches": [r.to_dict() for r in self.dispatches],
            "settlements": self.settlements,
            "verified": self.verified,
            "verification": self.verification,
        }


def message(
    text: str,
    *,
    message_id: str,
    at: datetime,
    referenced_message_id: str | None = None,
) -> RawDiscordMessage:
    return RawDiscordMessage(
        message_id=message_id,
        guild_id=GUILD,
        channel_id=CHANNEL_ID,
        author_id=AUTHOR,
        content=text,
        source_ts_utc=at,
        received_ts_utc=at,
        referenced_message_id=referenced_message_id,
    )


def _settle_one(
    pipeline: Pipeline, result: ScenarioResult, item: Dispatched, market_day: date
) -> None:
    """Apply one SENT item to the book immediately, so the next decision sees the truth."""
    if item.outcome != "SENT" or item.envelope is None or item.receipt is None:
        return
    if True:
        envelope = item.envelope
        receipt: Receipt = item.receipt
        is_buy = envelope.side == "BUY"
        settlement = pipeline.settle(
            contract_key=envelope.contract_key,
            generation=envelope.generation,
            quantity_delta=receipt.fill_qty if is_buy else -receipt.fill_qty,
            fill_price=receipt.fill_price or envelope.limit_price,
            market_day=market_day,
            final=not is_buy and envelope.effect_kind.value == "PROPOSE_CLOSE",
        )
        result.settlements.append(
            f"{envelope.side} {receipt.fill_qty}x {envelope.contract_key} "
            f"@ {receipt.fill_price} -> realized {settlement.realized} ({settlement.reason})"
        )


async def run_async(
    *,
    store: Store,
    messages: Iterable[RawDiscordMessage],
    quotes: QuoteSource,
    policy: Policy | None = None,
    sender: PaperSender | None = None,
    market_day: date | None = None,
) -> ScenarioResult:
    """Drive the real pipeline over a list of messages and report the trail.

    `quotes` may be a fixed map or a `(key, at) -> Quote` callable. The callable form is
    what a live feed needs: a quote has to be stamped at the moment of dispatch, and the
    authorization ladder (correctly) refuses anything stamped after the clock.
    """
    result = ScenarioResult()
    pipeline = Pipeline(store=store)
    market_day_seen: list[date] = [market_day or datetime.now(UTC).date()]
    active_policy = policy or Policy(live_submission_enabled=True)
    sender = sender or PaperSender()
    dispatcher = Dispatcher(
        store=store, sender=sender, policy=active_policy,
        on_filled=lambda item: _settle_one(pipeline, result, item, market_day_seen[0]),
    )

    for raw in messages:
        day = raw.source_ts_utc.astimezone(UTC).date()
        market_day = market_day or day
        event, effects = await pipeline.handle(raw)
        result.steps.append(
            Step(
                f"message {raw.message_id}",
                f"{event.kind.value}"
                + (f" {event.contract_key}" if event.contract_key else "")
                + (f" [{event.reason}]" if event.reason else ""),
            )
        )
        if effects:
            result.steps.append(
                Step("effects", ", ".join(f"{e.kind.value}:{e.reason}" for e in effects))
            )

        market_day_seen[0] = market_day
        # The dispatcher runs NOW, not at the signal's timestamp: a message arrives and then
        # delivery is attempted. Effects created microseconds ago must be due, and a quote is
        # judged against the clock at the moment of submission.
        at = datetime.now(UTC) + timedelta(seconds=1)
        live_keys = [p.contract_key for p in pipeline.state.positions.values()]
        if event.contract_key:
            live_keys.append(event.contract_key)
        if callable(quotes):
            quote_map: dict[str, Quote] = {k: quotes(k, at) for k in dict.fromkeys(live_keys)}
        else:
            quote_map = {k: quotes[k] for k in dict.fromkeys(live_keys) if k in quotes}

        report = dispatcher.dispatch_once(
            state=pipeline.state,
            now=at,
            quotes=quote_map,
            references={
                k: Decimal(str(event.referenced_price))
                for k in [event.contract_key or ""]
                if event.referenced_price is not None
            },
        )
        result.dispatches.append(report)
        for item in report.items:
            result.steps.append(
                Step("dispatch", f"{item.outcome} {item.contract_key} {item.detail}")
            )

    result.tca = report_from_store(store.load_executions())
    result.halted = pipeline.state.halted
    result.open_positions = len(
        [p for p in pipeline.state.positions.values() if p.status.value in
         {"PENDING_ENTRY", "OPEN", "CLOSING"}]
    )
    result.realized_usd = pipeline.state.risk.equity
    from .verify import verify
    check = verify(store, pipeline.state)
    result.verified = check.ok
    result.verification = check.to_dict()
    result.risk_reasons = tuple(
        f"{p.contract_key}:{p.last_reason}" for p in pipeline.state.positions.values()
        if p.last_reason
    )
    return result


def run(*, store: Store, messages: Iterable[RawDiscordMessage],
        quotes: QuoteSource, policy: Policy | None = None,
        sender: PaperSender | None = None, market_day: date | None = None) -> ScenarioResult:
    """Synchronous entry point for the CLI and for tests."""
    return asyncio.run(
        run_async(
            store=store, messages=messages, quotes=quotes, policy=policy,
            sender=sender, market_day=market_day,
        )
    )


def demo_messages(start: datetime | None = None) -> list[RawDiscordMessage]:
    """A small scripted session: one entry, a fill, an exit, and a second entry.

    The exit deliberately does not restate the contract - it relies on the reply/uniqueness
    association, which is what the channel actually does.
    """
    t0 = start or datetime.now(UTC).replace(microsecond=0)
    return [
        message("QQQ 719C TODAY @ 1.01", message_id="m1", at=t0),
        message("Closing runners 30%+", message_id="m2", at=t0 + timedelta(seconds=45),
                referenced_message_id="m1"),
        message("SPY 600P TODAY @ 0.90", message_id="m3", at=t0 + timedelta(minutes=2)),
    ]


DEMO_PRICES: dict[str, tuple[str, str]] = {
    "QQQ|CALL|719": ("1.00", "1.02"),
    "SPY|PUT|600": ("0.88", "0.90"),
}


def demo_quotes() -> QuoteSource:
    """A quote source for the demo: prices by contract prefix, stamped 100 ms before the
    moment it is asked for - which is what keeps them inside the freshness window."""

    def source(key: str, at: datetime) -> Quote:
        prefix = key.rsplit("|", 1)[0]
        bid, ask = DEMO_PRICES.get(prefix, ("1.00", "1.02"))
        return Quote(
            bid=Decimal(bid),
            ask=Decimal(ask),
            observed_ts_utc=at - timedelta(milliseconds=100),
        )

    return source

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from .association import associate_followup
from .config import MARKET_TZ
from .domain import BookState, Effect, RawDiscordMessage, SignalEvent
from .expiry import expire_on_roll
from .parser import parse_message
from .reducer import reduce_book
from .replay import replay
from .settlement import Settlement, settle_fill
from .store import Store


@dataclass(slots=True)
class Pipeline:
    store: Store
    allowed_author_ids: frozenset[str] | None = None
    state: BookState = field(init=False)
    last_market_day: date | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        # Event-sourced crash recovery: rebuild deterministic book state from
        # durable normalized events.
        self.state, historical_effects = replay(self.store.load_signals())
        self.last_market_day = self.state.risk.market_day
        # INSERT OR IGNORE re-materializes any effect lost by a crash after signal persistence.
        self.store.append_effects(historical_effects)

    async def handle(self, raw: RawDiscordMessage) -> tuple[SignalEvent, tuple[Effect, ...]]:
        self.store.append_raw(raw)
        event = parse_message(raw, self.allowed_author_ids)
        referenced_key = (
            self.store.contract_for_message(raw.referenced_message_id)
            if raw.referenced_message_id
            else None
        )
        event = associate_followup(event, self.state, referenced_key)

        # Day roll first: a contract that expired must not hold a slot against this
        # morning's entries. The sweep is a no-op until the market day actually moves.
        market_day = event.source_ts_utc.astimezone(MARKET_TZ).date()
        self.state, expiry_effects = expire_on_roll(
            self.state, self.last_market_day, market_day
        )
        if expiry_effects:
            self.store.append_effects(expiry_effects)
            self.store.heartbeat("expiry_sweep", released=len(expiry_effects))
        self.last_market_day = market_day

        inserted = self.store.append_signal(event)
        if not inserted:
            # Exact duplicate/reconnect delivery. State already includes this event.
            return event, ()
        self.state, effects = reduce_book(self.state, event)
        self.store.append_effects(effects)
        self.store.heartbeat(
            "pipeline",
            last_message_id=raw.message_id,
            last_event_id=event.event_id,
            effect_count=len(effects),
        )
        return event, effects

    def settle(
        self,
        *,
        contract_key: str,
        generation: int,
        quantity_delta: int,
        fill_price: Decimal,
        market_day: date,
        final: bool = False,
    ) -> Settlement:
        """Book a real fill: quantity, average cost, realised P&L, and the risk ledger.

        Lives on the pipeline because that is where the book is. A fill that updated
        positions but not the risk ledger would let the desk trade straight through its
        own daily stop - the two must move together or neither is real.
        """
        settlement = settle_fill(
            state=self.state,
            contract_key=contract_key,
            generation=generation,
            quantity_delta=quantity_delta,
            fill_price=fill_price,
            final=final,
        )
        self.state = settlement.state
        if settlement.realized or settlement.closed:
            self.state = replace(
                self.state,
                risk=self.state.risk.settled(settlement.realized, market_day),
            )
        return settlement

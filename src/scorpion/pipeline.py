from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal

from .association import associate_followup
from .config import MARKET_TZ
from .domain import BookState, Effect, PositionStatus, RawDiscordMessage, SignalEvent
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
        # A halt is durable state, so a restart resumes halted rather than quietly trading.
        halted, reason = self.store.halt_state()
        if halted and not self.state.halted:
            self.state = replace(self.state, halted=True)
            self.store.heartbeat("halt_recovered", reason=reason)
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
        pre_sweep = self.state.positions
        self.state, expiry_effects = expire_on_roll(
            self.state, self.last_market_day, market_day
        )
        if expiry_effects:
            self.store.append_effects(expiry_effects)
            self.store.heartbeat("expiry_sweep", released=len(expiry_effects))
        # The sweep closes positions with no signal and no fill behind it. Written to the applied
        # log here, at the moment it happens, or the book contains state its own log cannot
        # explain - which is exactly the divergence `verify` reported at seq 22.
        for key, prior in pre_sweep.items():
            after = self.state.positions.get(key)
            released = after is None or (
                after.status is PositionStatus.CLOSED and prior.status is not PositionStatus.CLOSED
            )
            if released:
                self.store.append_applied(
                    kind="expire",
                    payload_json=json.dumps({
                        "contract_key": key,
                        "generation": prior.generation,
                        "quantity": prior.quantity,
                        "market_day": market_day.isoformat(),
                        "swept_on": event.source_ts_utc.isoformat(),
                    }, sort_keys=True),
                    ts=event.source_ts_utc,
                    contract_key=key,
                )
        self.last_market_day = market_day

        inserted = self.store.append_signal(event)
        if not inserted:
            # Exact duplicate/reconnect delivery. State already includes this event.
            return event, ()
        before = self.state.positions
        self.state, effects = reduce_book(self.state, event)
        # The reducer is allowed to release positions on its own - the expiry sweep does, when a
        # day roll passes a contract's expiry. That is a real state transition with no signal
        # and no fill behind it, and if it is not written to the log the book contains state its
        # own log cannot explain. Captured by DIFF rather than by knowing how the sweep works,
        # so any future in-reducer maintenance is recorded for free.
        for key, prior in before.items():
            after = self.state.positions.get(key)
            released = after is None or (
                after.status is PositionStatus.CLOSED and prior.status is not PositionStatus.CLOSED
            )
            if released:
                self.store.append_applied(
                    kind="expire",
                    payload_json=json.dumps({
                        "contract_key": key,
                        "generation": prior.generation,
                        "quantity": prior.quantity,
                        "market_day": event.source_ts_utc.date().isoformat(),
                    }, sort_keys=True),
                    ts=event.source_ts_utc,
                    contract_key=key,
                )
        self.store.append_applied(
            kind="signal",
            payload_json=self.store._signal_payload(event),
            ts=event.source_ts_utc,
            contract_key=event.contract_key,
        )
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
        self.store.append_applied(
            kind="fill",
            payload_json=json.dumps({
                "contract_key": contract_key,
                "generation": generation,
                "quantity_delta": quantity_delta,
                "fill_price": str(fill_price),
                "final": final,
                "market_day": market_day.isoformat(),
            }, sort_keys=True),
            ts=datetime.now(UTC),
            contract_key=contract_key,
        )
        if settlement.realized or settlement.closed:
            self.state = replace(
                self.state,
                risk=self.state.risk.settled(settlement.realized, market_day),
            )
        return settlement

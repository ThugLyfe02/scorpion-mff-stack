from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from .fill_calibration import FillPredictionRecord, record_fill_prediction
from .microstructure import (
    MicroFillEnvelope,
    OptionMarketEvent,
    OptionMicrostructureTape,
    ns_from_datetime,
)


class ShadowIntentSide(StrEnum):
    BUY_LIMIT = "BUY_LIMIT"
    SELL_AGGRESSIVE = "SELL_AGGRESSIVE"


@dataclass(frozen=True, slots=True)
class ShadowExecutionIntent:
    intent_id: str
    contract_key: str
    side: ShadowIntentSide
    quantity: int
    order_arrival_ts_utc: datetime
    limit_price: Decimal | None = None
    observation_window: timedelta = timedelta(seconds=5)
    market_quote_max_age: timedelta = timedelta(seconds=1)

    def __post_init__(self) -> None:
        if not self.intent_id.strip() or not self.contract_key.strip():
            raise ValueError("intent_id and contract_key are required")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if (
            self.side is ShadowIntentSide.BUY_LIMIT
            and (self.limit_price is None or self.limit_price <= 0)
        ):
            raise ValueError("BUY_LIMIT requires a positive limit_price")
        if self.observation_window < timedelta(0):
            raise ValueError("observation_window cannot be negative")
        if self.market_quote_max_age < timedelta(0):
            raise ValueError("market_quote_max_age cannot be negative")

    @property
    def deadline_ts_utc(self) -> datetime:
        return self.order_arrival_ts_utc + self.observation_window


@dataclass(frozen=True, slots=True)
class ShadowExecutionResult:
    intent: ShadowExecutionIntent
    fill: MicroFillEnvelope
    events_observed: int
    first_event_ns: int | None
    last_event_ns: int | None


class LiveShadowRecorder:
    """In-memory live/paper market-event recorder using the historical fill engine.

    The recorder never submits orders. It accumulates observed market events and evaluates a
    prepared intent using :class:`OptionMicrostructureTape`, guaranteeing that live shadow and
    historical research use the same execution semantics.
    """

    def __init__(self) -> None:
        self._events: list[OptionMarketEvent] = []

    def on_event(self, event: OptionMarketEvent) -> None:
        self._events.append(event)

    @property
    def events_observed(self) -> int:
        return len(self._events)

    def evaluate(self, intent: ShadowExecutionIntent) -> ShadowExecutionResult:
        arrival_ns = ns_from_datetime(intent.order_arrival_ts_utc)
        deadline_ns = ns_from_datetime(intent.deadline_ts_utc)
        relevant = tuple(
            event
            for event in self._events
            if event.contract_key == intent.contract_key
            and event.ts_event_ns <= deadline_ns
        )
        tape = OptionMicrostructureTape(relevant)
        if intent.side is ShadowIntentSide.BUY_LIMIT:
            if intent.limit_price is None:
                raise RuntimeError("BUY_LIMIT intent lost its limit price")
            fill = tape.simulate_limit_buy(
                intent.contract_key,
                order_arrival_ts_utc=intent.order_arrival_ts_utc,
                quantity=intent.quantity,
                limit_price=intent.limit_price,
                max_wait=intent.observation_window,
                market_quote_max_age=intent.market_quote_max_age,
            )
        else:
            fill = tape.simulate_aggressive_sell(
                intent.contract_key,
                order_arrival_ts_utc=intent.order_arrival_ts_utc,
                quantity=intent.quantity,
                market_quote_max_age=intent.market_quote_max_age,
            )
        in_window = [
            event
            for event in relevant
            if arrival_ns <= event.ts_event_ns <= deadline_ns
        ]
        return ShadowExecutionResult(
            intent=intent,
            fill=fill,
            events_observed=len(in_window),
            first_event_ns=in_window[0].ts_event_ns if in_window else None,
            last_event_ns=in_window[-1].ts_event_ns if in_window else None,
        )

    def evaluate_and_record(
        self,
        intent: ShadowExecutionIntent,
        *,
        event_id: str,
        calibration_db: str | Path,
    ) -> tuple[ShadowExecutionResult, FillPredictionRecord]:
        """Evaluate a shadow intent and freeze its predicted fill interval for later calibration."""
        result = self.evaluate(intent)
        prediction = record_fill_prediction(
            calibration_db,
            intent_id=intent.intent_id,
            event_id=event_id,
            contract_key=intent.contract_key,
            fill=result.fill,
            order_arrival_ts_utc=intent.order_arrival_ts_utc,
            observation_deadline_ts_utc=intent.deadline_ts_utc,
        )
        return result, prediction


EventSink = Callable[[OptionMarketEvent], Awaitable[None] | None]


async def replay_market_events_wall_clock(
    events: Sequence[OptionMarketEvent],
    sink: EventSink,
    *,
    speed: float = 1.0,
    max_sleep: float = 0.250,
) -> None:
    """Replay provider receive-time order against a wall clock.

    ``speed=1`` preserves observed inter-arrival timing. ``speed=0`` disables sleeping while
    preserving the exact receive-time order, which is useful for CI. Large speed values provide
    accelerated integration tests without changing event ordering.
    """
    if speed < 0:
        raise ValueError("speed cannot be negative")
    if max_sleep < 0:
        raise ValueError("max_sleep cannot be negative")
    ordered = sorted(
        events,
        key=lambda event: (event.ts_recv_ns, event.ts_event_ns, event.sequence),
    )
    if not ordered:
        return
    base_recv_ns = ordered[0].ts_recv_ns
    wall_started = time.monotonic()
    for event in ordered:
        if speed > 0:
            target_elapsed = ((event.ts_recv_ns - base_recv_ns) / 1_000_000_000) / speed
            remaining = target_elapsed - (time.monotonic() - wall_started)
            while remaining > 0:
                await asyncio.sleep(min(remaining, max_sleep or remaining))
                remaining = target_elapsed - (time.monotonic() - wall_started)
        result = sink(event)
        if result is not None:
            await result

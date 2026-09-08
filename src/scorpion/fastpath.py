from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from .broker import ExecutionMode, ExecutionResult, PaperBroker, Quote, ReviewOnlyBroker
from .decision_packet import DecisionDisposition, OperatorDecisionPacket
from .domain import Effect, EffectKind, SignalEvent
from .pricing import entry_is_stale, entry_limit


class FastPathStatus(StrEnum):
    PAPER_READY = "PAPER_READY"
    AWAITING_HUMAN_AUTHORIZATION = "AWAITING_HUMAN_AUTHORIZATION"
    WAITING_FOR_LIMIT = "WAITING_FOR_LIMIT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED_STRATEGY = "BLOCKED_STRATEGY"
    BLOCKED_SYSTEM = "BLOCKED_SYSTEM"
    QUOTE_UNAVAILABLE = "QUOTE_UNAVAILABLE"
    QUOTE_STALE = "QUOTE_STALE"
    ENTRY_DISLOCATION = "ENTRY_DISLOCATION"
    NOOP = "NOOP"


@dataclass(frozen=True, slots=True)
class PreparedExecutionIntent:
    event_id: str
    effect: Effect
    mode: ExecutionMode
    status: FastPathStatus
    quantity: int
    limit_price: Decimal | None
    quote: Quote | None
    prepared_ts_utc: datetime
    preparation_latency_us: int
    note: str = ""


class QuoteCache:
    """Tiny thread-safe quote cache intended to be fed by a separate market-data stream."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quotes: dict[str, Quote] = {}

    def update(
        self,
        contract_key: str,
        *,
        bid: Decimal,
        ask: Decimal,
        observed_ts_utc: datetime,
    ) -> None:
        if not contract_key:
            raise ValueError("contract_key is required")
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError("invalid quote")
        quote = Quote(bid, ask, observed_ts_utc.astimezone(UTC))
        with self._lock:
            self._quotes[contract_key] = quote

    def get(
        self,
        contract_key: str,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(seconds=1),
    ) -> Quote | None:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        with self._lock:
            quote = self._quotes.get(contract_key)
        if quote is None:
            return None
        age = now - quote.observed_ts_utc.astimezone(UTC)
        if age < timedelta(0) or age > max_age:
            return None
        return quote


class FastPathPreparer:
    """Code-only deterministic preparation path after an accepted Discord transition.

    No LLM call and no live broker API exists here. REVIEW_ONLY produces a ready authorization
    instruction; PAPER can fill through PaperBroker for latency/replay tests.
    """

    def __init__(
        self,
        quote_cache: QuoteCache,
        *,
        mode: ExecutionMode = ExecutionMode.REVIEW_ONLY,
        quote_max_age: timedelta = timedelta(seconds=1),
    ) -> None:
        self.quote_cache = quote_cache
        self.mode = mode
        self.quote_max_age = quote_max_age
        self._paper = PaperBroker()
        self._review = ReviewOnlyBroker()

    def prepare(
        self,
        event: SignalEvent,
        effect: Effect,
        packet: OperatorDecisionPacket,
        *,
        quantity: int,
        now: datetime | None = None,
    ) -> PreparedExecutionIntent:
        started_ns = time.perf_counter_ns()
        now = (now or datetime.now(UTC)).astimezone(UTC)

        def finish(
            status: FastPathStatus,
            *,
            limit_price: Decimal | None = None,
            quote: Quote | None = None,
            note: str = "",
        ) -> PreparedExecutionIntent:
            return PreparedExecutionIntent(
                event_id=event.event_id,
                effect=effect,
                mode=self.mode,
                status=status,
                quantity=quantity,
                limit_price=limit_price,
                quote=quote,
                prepared_ts_utc=now,
                preparation_latency_us=max(
                    0,
                    (time.perf_counter_ns() - started_ns) // 1_000,
                ),
                note=note,
            )

        if packet.disposition is DecisionDisposition.BLOCKED_SYSTEM:
            return finish(FastPathStatus.BLOCKED_SYSTEM, note="operational mode halted")
        if packet.disposition is DecisionDisposition.BLOCKED_STRATEGY:
            return finish(
                FastPathStatus.BLOCKED_STRATEGY,
                note=packet.eligibility_reason or "strategy research-only",
            )
        if packet.disposition is DecisionDisposition.REVIEW_REQUIRED:
            return finish(FastPathStatus.REVIEW_REQUIRED, note="operator review required")
        if effect.kind in {EffectKind.REVIEW, EffectKind.HALT}:
            return finish(FastPathStatus.NOOP, note=effect.reason)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        contract_key = effect.contract_key
        if contract_key is None:
            return finish(FastPathStatus.REVIEW_REQUIRED, note="effect missing contract")
        quote = self.quote_cache.get(
            contract_key,
            now=now,
            max_age=self.quote_max_age,
        )
        if quote is None:
            return finish(FastPathStatus.QUOTE_UNAVAILABLE)

        limit_price: Decimal | None
        if effect.kind in {EffectKind.PROPOSE_OPEN, EffectKind.PROPOSE_ADD}:
            reference = event.referenced_price
            if reference is not None:
                if entry_is_stale(reference, quote.ask):
                    return finish(
                        FastPathStatus.ENTRY_DISLOCATION,
                        quote=quote,
                        note="live ask is beyond the configured source-reference dislocation",
                    )
                limit_price = entry_limit(reference, quote.ask)
                if quote.ask > limit_price:
                    return finish(
                        FastPathStatus.WAITING_FOR_LIMIT,
                        limit_price=limit_price,
                        quote=quote,
                        note="limit rests at source-reference markup cap",
                    )
            else:
                limit_price = quote.ask
        elif effect.kind in {EffectKind.PROPOSE_TRIM, EffectKind.PROPOSE_CLOSE}:
            limit_price = quote.bid
        else:
            return finish(FastPathStatus.NOOP, quote=quote)

        status = (
            FastPathStatus.PAPER_READY
            if self.mode is ExecutionMode.PAPER
            else FastPathStatus.AWAITING_HUMAN_AUTHORIZATION
        )
        return finish(status, limit_price=limit_price, quote=quote)

    def dispatch(self, intent: PreparedExecutionIntent) -> ExecutionResult:
        if intent.status is FastPathStatus.PAPER_READY:
            return self._paper.execute(
                intent.effect,
                intent.quantity,
                intent.limit_price,
            )
        if intent.status is FastPathStatus.AWAITING_HUMAN_AUTHORIZATION:
            return self._review.execute(
                intent.effect,
                intent.quantity,
                intent.limit_price,
            )
        raise ValueError(f"intent is not dispatchable: {intent.status.value}")

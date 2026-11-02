from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from .broker import (
    ExecutionMode,
    ExecutionResult,
    PaperBroker,
    Quote,
    ReviewOnlyBroker,
    StaleQuote,
)
from .decision_packet import DecisionDisposition, OperatorDecisionPacket
from .domain import Effect, EffectKind, SignalEvent
from .pricing import entry_is_stale, entry_limit


class QuoteCacheStatus(StrEnum):
    FRESH = "FRESH"
    MISSING = "MISSING"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class QuoteLookup:
    status: QuoteCacheStatus
    quote: Quote | None
    age_ms: float | None
    reason: str = ""


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


class QuoteSource(Protocol):
    def lookup(
        self,
        contract_key: str,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(seconds=1),
    ) -> QuoteLookup: ...

    def get(
        self,
        contract_key: str,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(seconds=1),
    ) -> Quote | None: ...


class QuoteCache:
    """Per-symbol quote cache fed by the market-data task.

    One dict entry per symbol; a lookup never falls back to another symbol. Two read paths:

    * :meth:`lookup`/:meth:`get` — legacy wall-clock API used by :class:`FastPathPreparer`.
    * :meth:`fresh` — the exec-core decision path: monotonic-clock age, millisecond guard, and a
      typed :class:`StaleQuote` instead of ``None``.

    The writer and the decision loop share one asyncio thread in the paper session; the lock only
    matters for legacy threaded callers and is uncontended there.
    """

    def __init__(self, *, mono_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._lock = threading.RLock()
        self._quotes: dict[str, Quote] = {}
        self._mono_ns = mono_ns
        self._last_message_mono_ns: int | None = None

    def update(
        self,
        contract_key: str,
        *,
        bid: Decimal,
        ask: Decimal,
        observed_ts_utc: datetime,
        last: Decimal | None = None,
        ts_event_ns: int | None = None,
        recv_mono_ns: int | None = None,
    ) -> None:
        if not contract_key:
            raise ValueError("contract_key is required")
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError("invalid quote")
        received = self._mono_ns() if recv_mono_ns is None else recv_mono_ns
        quote = Quote(
            bid,
            ask,
            observed_ts_utc.astimezone(UTC),
            last=last,
            ts_event_ns=ts_event_ns,
            recv_mono_ns=received,
        )
        with self._lock:
            self._quotes[contract_key] = quote
            self._last_message_mono_ns = received

    def note_message(self, recv_mono_ns: int | None = None) -> None:
        """Record subscription liveness for a message that carried no usable quote."""

        with self._lock:
            self._last_message_mono_ns = self._mono_ns() if recv_mono_ns is None else recv_mono_ns

    @property
    def last_message_mono_ns(self) -> int | None:
        return self._last_message_mono_ns

    def symbols(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._quotes)

    def age_ms(self, symbol: str, *, now_mono_ns: int | None = None) -> float | None:
        quote = self._quotes.get(symbol)
        if quote is None or quote.recv_mono_ns is None:
            return None
        now = self._mono_ns() if now_mono_ns is None else now_mono_ns
        return (now - quote.recv_mono_ns) / 1_000_000.0

    def fresh(
        self,
        symbol: str,
        *,
        max_age_ms: int,
        now_mono_ns: int | None = None,
    ) -> Quote | StaleQuote:
        quote = self._quotes.get(symbol)
        if quote is None:
            return StaleQuote(symbol, None, "unknown_symbol")
        if quote.recv_mono_ns is None:
            return StaleQuote(symbol, None, "no_monotonic_receive_time")
        now = self._mono_ns() if now_mono_ns is None else now_mono_ns
        age_ms = (now - quote.recv_mono_ns) / 1_000_000.0
        if age_ms < 0 or age_ms > max_age_ms:
            return StaleQuote(symbol, age_ms, f"stale_age_ms={age_ms:.1f}>{max_age_ms}")
        return quote

    def lookup(
        self,
        contract_key: str,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(seconds=1),
    ) -> QuoteLookup:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        with self._lock:
            quote = self._quotes.get(contract_key)
        if quote is None:
            return QuoteLookup(QuoteCacheStatus.MISSING, None, None)
        age = now - quote.observed_ts_utc.astimezone(UTC)
        age_ms = age.total_seconds() * 1_000.0
        if age < timedelta(0) or age > max_age:
            return QuoteLookup(QuoteCacheStatus.STALE, quote, age_ms)
        return QuoteLookup(QuoteCacheStatus.FRESH, quote, age_ms)

    def get(
        self,
        contract_key: str,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(seconds=1),
    ) -> Quote | None:
        lookup = self.lookup(contract_key, now=now, max_age=max_age)
        return lookup.quote if lookup.status is QuoteCacheStatus.FRESH else None


class FastPathPreparer:
    """Code-only deterministic preparation path after an accepted Discord transition.

    No LLM call and no live broker API exists here. REVIEW_ONLY produces a ready authorization
    instruction; PAPER can fill through PaperBroker for latency/replay tests.
    """

    def __init__(
        self,
        quote_cache: QuoteSource,
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
        lookup = self.quote_cache.lookup(
            contract_key,
            now=now,
            max_age=self.quote_max_age,
        )
        if lookup.status is QuoteCacheStatus.MISSING:
            return finish(FastPathStatus.QUOTE_UNAVAILABLE, note=lookup.reason)
        if lookup.status is QuoteCacheStatus.STALE:
            return finish(
                FastPathStatus.QUOTE_STALE,
                quote=lookup.quote,
                note=(
                    f"cached quote age_ms={lookup.age_ms:.1f}"
                    if lookup.age_ms is not None
                    else lookup.reason
                ),
            )
        quote = lookup.quote
        if quote is None:
            raise RuntimeError("fresh quote lookup returned no quote")

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

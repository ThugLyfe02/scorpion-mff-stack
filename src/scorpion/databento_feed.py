"""Databento market data for the paper session: live feed with bounded reconnect, and a
cost-guarded historical pull. Databento is the only market-data source; the API key comes from
``DATABENTO_API_KEY`` and is never logged, printed or placed in an exception message.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from .config import DATABENTO_COST_CAP_USD, STALE_POLICY
from .fastpath import QuoteCache
from .feed_integrity import FeedContinuityMode, FeedIntegrityReport, evaluate_feed_integrity

LOGGER = logging.getLogger(__name__)
API_KEY_ENV = "DATABENTO_API_KEY"
_PRICE_SCALE = Decimal("1e-9")
_UNDEF_PRICE = 2**63 - 1


def redact(text: str) -> str:
    key = os.environ.get(API_KEY_ENV, "")
    return text.replace(key, "***") if key else text


class SecretRedactingFilter(logging.Filter):
    """Belt-and-braces: scrub the API key from any log record that reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        key = os.environ.get(API_KEY_ENV, "")
        if key:
            message = record.getMessage()
            if key in message:
                record.msg = message.replace(key, "***")
                record.args = ()
        return True


def api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(f"{API_KEY_ENV} is not set")
    return key


# ------------------------------------------------------------------------------------ live feed


@dataclass(frozen=True, slots=True)
class FeedMessage:
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    ts_event_ns: int
    bid_size: int | None = None
    ask_size: int | None = None
    publisher_id: int | None = None
    instrument_id: int | None = None
    provider_sequence: int | None = None


class FeedCaptureError(RuntimeError):
    """Evidence sink failure must escape reconnect handling and halt the paper session."""


class FeedClient(Protocol):
    def stream(self, symbols: Sequence[str], *, snapshot: bool) -> AsyncIterator[FeedMessage]: ...

    def add_symbols(self, symbols: Sequence[str]) -> None: ...


@dataclass(frozen=True, slots=True)
class ReconnectSchedule:
    """Exponential backoff with equal jitter: delay_n in [d_n/2, d_n], d_n = min(cap, base*2^n)."""

    base_s: float = 0.5
    cap_s: float = 30.0

    def bounds(self, attempt: int) -> tuple[float, float]:
        ceiling = min(self.cap_s, self.base_s * (2 ** max(0, attempt)))
        return ceiling / 2.0, ceiling

    def delay(self, attempt: int, rng: random.Random) -> float:
        low, high = self.bounds(attempt)
        return rng.uniform(low, high)


@dataclass
class FeedStats:
    connects: int = 0
    disconnects: int = 0
    snapshots_requested: int = 0
    messages: int = 0
    rejected_time: int = 0
    rejected_order: int = 0
    rejected_quote: int = 0
    rejected_symbol: int = 0
    timestamp_conflicts: int = 0
    first_usable_recv_wall_ns: int | None = None
    delays_s: list[float] = field(default_factory=list)


class DatabentoFeed:
    """Writes the live stream into the shared :class:`QuoteCache`; nothing else."""

    def __init__(
        self,
        client: FeedClient,
        cache: QuoteCache,
        symbols: Sequence[str],
        *,
        schedule: ReconnectSchedule | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        mono_ns: Callable[[], int] = time.monotonic_ns,
        wall_ns: Callable[[], int] = time.time_ns,
        on_skew: Callable[[float, int], None] | None = None,
        max_disconnects: int | None = None,
        max_event_age_ms: int = STALE_POLICY.quote_max_age_ms,
        on_observation: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        import asyncio

        self._client = client
        self._cache = cache
        self._symbols = tuple(dict.fromkeys(symbols))
        if not self._symbols or any(not symbol for symbol in self._symbols):
            raise ValueError("nonempty subscription universe required")
        self._subscribed = set(self._symbols)
        self._schedule = schedule or ReconnectSchedule()
        self._rng = rng or random.Random()
        self._sleep = sleep or asyncio.sleep
        self._mono_ns = mono_ns
        self._wall_ns = wall_ns
        self._on_skew = on_skew
        self._on_observation = on_observation
        self._max_disconnects = max_disconnects
        if max_event_age_ms <= 0:
            raise ValueError("max_event_age_ms must be positive")
        self._max_event_age_ns = max_event_age_ms * 1_000_000
        self._event_watermark: dict[str, int] = {}
        self._event_payload: dict[str, tuple[object, ...]] = {}
        self.stats = FeedStats()

    def add_symbol(self, symbol: str) -> None:
        """Subscribe a newly called ticker (2 minutes of slack before its entry minute)."""

        if not symbol:
            raise ValueError("empty subscription symbol")
        if symbol in self._subscribed:
            return
        # A failed subscribe must not falsely mark a symbol as subscribed.
        self._client.add_symbols([symbol])
        self._symbols = (*self._symbols, symbol)
        self._subscribed.add(symbol)

    def _observe(self, record: dict[str, object]) -> None:
        if self._on_observation is not None:
            try:
                self._on_observation(record)
            except Exception as exc:
                raise FeedCaptureError("market_observation_sink_failed") from exc

    def _apply(self, message: FeedMessage) -> None:
        received = self._mono_ns()
        self.stats.messages += 1
        if message.symbol not in self._subscribed:
            self.stats.rejected_symbol += 1
            return
        received_wall = self._wall_ns()
        age_ns = received_wall - message.ts_event_ns
        if self._on_skew is not None:
            self._on_skew(age_ns / 1_000_000.0, received)
        # A replayed, delayed or future exchange event cannot refresh an executable quote.
        if message.ts_event_ns <= 0 or not 0 <= age_ns <= self._max_event_age_ns:
            self.stats.rejected_time += 1
            self._cache.invalidate(message.symbol)
            self._observe({"kind": "market_gap", "symbol": message.symbol,
                           "reason": "invalid_event_time", "recv_wall_ns": received_wall})
            return
        previous = self._event_watermark.get(message.symbol, -1)
        payload = (str(message.bid), str(message.ask), str(message.last),
                   message.bid_size, message.ask_size, message.publisher_id, message.instrument_id)
        if message.ts_event_ns <= previous:
            self.stats.rejected_order += 1
            if (
                message.ts_event_ns == previous
                and payload != self._event_payload.get(message.symbol)
            ):
                # Without a provider sequence tie-breaker, neither version is authoritative.
                # In particular, a same-timestamp withdrawal cannot leave a price actionable.
                self.stats.timestamp_conflicts += 1
                self._cache.invalidate(message.symbol)
                self._observe({"kind": "market_gap", "symbol": message.symbol,
                               "reason": "timestamp_conflict", "recv_wall_ns": received_wall})
            return
        self._event_watermark[message.symbol] = message.ts_event_ns
        self._event_payload[message.symbol] = payload
        bid, ask = message.bid, message.ask
        if (
            bid is None or ask is None or not bid.is_finite() or not ask.is_finite()
            or bid <= 0 or ask <= 0 or ask < bid
        ):
            self.stats.rejected_quote += 1
            self._cache.invalidate(message.symbol)
            self._cache.note_message(received)
            self._observe({"kind": "market_withdrawal", "symbol": message.symbol,
                           "reason": "invalid_quote", "recv_wall_ns": received_wall,
                           "ts_event_ns": message.ts_event_ns})
            return
        last = message.last
        if last is not None and (not last.is_finite() or last <= 0):
            last = None
        self._cache.update(
            message.symbol,
            bid=bid,
            ask=ask,
            last=last,
            observed_ts_utc=datetime.fromtimestamp(message.ts_event_ns / 1e9, tz=UTC),
            ts_event_ns=message.ts_event_ns,
            recv_mono_ns=received,
            source_age_ns=age_ns,
        )
        if self.stats.first_usable_recv_wall_ns is None:
            self.stats.first_usable_recv_wall_ns = received_wall
        self._observe({
            "kind": "market_quote", "symbol": message.symbol,
            "ts_event_ns": message.ts_event_ns, "recv_wall_ns": received_wall,
            "recv_mono_ns": received, "bid": str(bid), "ask": str(ask),
            "bid_size": message.bid_size, "ask_size": message.ask_size,
            "publisher_id": message.publisher_id, "instrument_id": message.instrument_id,
            "provider_sequence": message.provider_sequence,
        })

    async def run(self) -> None:
        attempt = 0
        while True:
            snapshot = self.stats.connects > 0
            if snapshot:
                self.stats.snapshots_requested += 1
            self.stats.connects += 1
            received_any = False
            try:
                async for message in self._client.stream(self._symbols, snapshot=snapshot):
                    received_any = True
                    self._apply(message)
            except FeedCaptureError:
                raise
            except Exception as exc:
                LOGGER.warning("databento feed dropped: %s", redact(type(exc).__name__))
            finally:
                self._cache.invalidate_all()
            self.stats.disconnects += 1
            self._observe({"kind": "market_gap", "symbol": None,
                           "reason": "transport_disconnect", "recv_wall_ns": self._wall_ns()})
            if self._max_disconnects is not None and self.stats.disconnects >= self._max_disconnects:
                return
            if received_any:
                attempt = 0
            delay = self._schedule.delay(attempt, self._rng)
            self.stats.delays_s.append(delay)
            attempt += 1
            await self._sleep(delay)

    def integrity_report(self) -> FeedIntegrityReport:
        # Each disconnect is a provider transport gap until a snapshot proves continuity.
        return evaluate_feed_integrity(
            (),
            continuity_mode=FeedContinuityMode.FILTERED_UNPROVEN,
            provider_gap_events=self.stats.disconnects,
        )


class DatabentoLiveClient:
    """Adapter over the official ``databento.Live`` client (optional dependency)."""

    def __init__(self, *, dataset: str = "XNAS.ITCH", schema: str = "mbp-1") -> None:
        if schema not in ("mbp-1", "cmbp-1"):
            raise ValueError("top-of-book adapter requires mbp-1 or cmbp-1")
        self.dataset = dataset
        self.schema = schema
        self._active: Any = None

    def add_symbols(self, symbols: Sequence[str]) -> None:
        # databento.Live accepts additional subscribe() calls on a running session.
        if self._active is not None:
            self._active.subscribe(
                dataset=self.dataset, schema=self.schema, symbols=list(symbols), snapshot=False
            )

    async def stream(self, symbols: Sequence[str], *, snapshot: bool) -> AsyncIterator[FeedMessage]:
        import databento  # optional live dependency

        # Databento snapshots are MBO-only; MBP reconnects never imply restored continuity.
        client: Any = databento.Live(key=api_key(), slow_reader_behavior="warn")
        self._active = client
        try:
            client.subscribe(
                dataset=self.dataset, schema=self.schema, symbols=list(symbols), snapshot=False
            )
            async for record in client:
                kind = type(record).__name__
                if kind == "ErrorMsg":
                    # Includes skipped records, unresolved symbols and failed subscriptions.
                    raise RuntimeError("databento_provider_error")
                if kind == "SystemMsg" and getattr(record, "code", None) == 2:
                    raise RuntimeError("databento_slow_reader_warning")
                symbol = client.symbology_map.get(getattr(record, "instrument_id", -1))
                levels = getattr(record, "levels", None)
                if not symbol or not levels:
                    continue
                level = levels[0]
                action = getattr(record, "action", None)
                # MBP action prices are not all trade prints. Never use add/cancel price as last.
                action = getattr(action, "value", action)
                price = (
                    getattr(record, "price", _UNDEF_PRICE)
                    if action in ("T", b"T") else _UNDEF_PRICE
                )
                yield FeedMessage(
                    symbol=str(symbol),
                    bid=_px(level.bid_px),
                    ask=_px(level.ask_px),
                    last=_px(price),
                    ts_event_ns=int(record.ts_event),
                    bid_size=int(level.bid_sz), ask_size=int(level.ask_sz),
                    publisher_id=int(record.publisher_id), instrument_id=int(record.instrument_id),
                    provider_sequence=int(record.sequence),
                )
        finally:
            self._active = None
            try:
                client.stop()
            except Exception as exc:
                LOGGER.warning("databento stop failed: %s", type(exc).__name__)


def _px(raw: int) -> Decimal | None:
    if raw in (_UNDEF_PRICE, 0):
        return None
    return Decimal(raw) * _PRICE_SCALE


# ------------------------------------------------------------------------- historical cost guard


class CostGuardRefused(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PullRequest:
    dataset: str
    schema: str
    symbols: tuple[str, ...]
    start: str
    end: str
    stype_in: str = "raw_symbol"

    def params(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "schema": self.schema,
            "symbols": list(self.symbols),
            "start": self.start,
            "end": self.end,
            "stype_in": self.stype_in,
        }


@dataclass(frozen=True, slots=True)
class PullDecision:
    estimated_cost_usd: Decimal
    cap_usd: Decimal
    approved: bool
    executed: bool


def historical_client() -> Any:
    import databento  # optional dependency

    return databento.Historical(key=api_key())


def guarded_pull(
    client: Any,
    request: PullRequest,
    *,
    approve_cost: bool = False,
    estimate_only: bool = False,
    cap_usd: Decimal = DATABENTO_COST_CAP_USD,
    sink: Callable[[Any], None] | None = None,
) -> PullDecision:
    """Every historical pull calls ``metadata.get_cost`` first and refuses above the cap."""

    try:
        cost = Decimal(str(client.metadata.get_cost(**request.params())))
    except Exception as exc:
        raise RuntimeError(f"cost estimate failed: {redact(str(exc))}") from None
    over_cap = cost > cap_usd
    if estimate_only:
        return PullDecision(cost, cap_usd, approved=not over_cap or approve_cost, executed=False)
    if over_cap and not approve_cost:
        raise CostGuardRefused(
            f"estimated ${cost:.2f} exceeds the ${cap_usd:.2f} cap; rerun with --approve-cost "
            "only after the owner approves this spend"
        )
    try:
        data = client.timeseries.get_range(**request.params())
    except Exception as exc:
        raise RuntimeError(f"historical pull failed: {redact(str(exc))}") from None
    if sink is not None:
        sink(data)
    return PullDecision(cost, cap_usd, approved=True, executed=True)

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

from .config import DATABENTO_COST_CAP_USD
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
    ) -> None:
        import asyncio

        self._client = client
        self._cache = cache
        self._symbols = tuple(symbols)
        self._schedule = schedule or ReconnectSchedule()
        self._rng = rng or random.Random()
        self._sleep = sleep or asyncio.sleep
        self._mono_ns = mono_ns
        self._wall_ns = wall_ns
        self._on_skew = on_skew
        self._max_disconnects = max_disconnects
        self.stats = FeedStats()

    def add_symbol(self, symbol: str) -> None:
        """Subscribe a newly called ticker (2 minutes of slack before its entry minute)."""

        if symbol in self._symbols:
            return
        self._symbols = (*self._symbols, symbol)
        self._client.add_symbols([symbol])

    def _apply(self, message: FeedMessage) -> None:
        received = self._mono_ns()
        self.stats.messages += 1
        if self._on_skew is not None:
            self._on_skew((self._wall_ns() - message.ts_event_ns) / 1_000_000.0, received)
        bid, ask = message.bid, message.ask
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            self._cache.note_message(received)
            return
        self._cache.update(
            message.symbol,
            bid=bid,
            ask=ask,
            last=message.last,
            observed_ts_utc=datetime.fromtimestamp(message.ts_event_ns / 1e9, tz=UTC),
            ts_event_ns=message.ts_event_ns,
            recv_mono_ns=received,
        )

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
            except Exception as exc:
                LOGGER.warning("databento feed dropped: %s", redact(type(exc).__name__))
            self.stats.disconnects += 1
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
        self.dataset = dataset
        self.schema = schema
        self._active: Any = None

    def add_symbols(self, symbols: Sequence[str]) -> None:
        # databento.Live accepts additional subscribe() calls on a running session.
        if self._active is not None:
            self._active.subscribe(
                dataset=self.dataset, schema=self.schema, symbols=list(symbols), snapshot=True
            )

    async def stream(self, symbols: Sequence[str], *, snapshot: bool) -> AsyncIterator[FeedMessage]:
        import databento  # optional live dependency

        client: Any = databento.Live(key=api_key())
        self._active = client
        try:
            client.subscribe(
                dataset=self.dataset, schema=self.schema, symbols=list(symbols), snapshot=snapshot
            )
            async for record in client:
                symbol = client.symbology_map.get(getattr(record, "instrument_id", -1))
                levels = getattr(record, "levels", None)
                if not symbol or not levels:
                    continue
                level = levels[0]
                price = getattr(record, "price", _UNDEF_PRICE)
                yield FeedMessage(
                    symbol=str(symbol),
                    bid=_px(level.bid_px),
                    ask=_px(level.ask_px),
                    last=_px(price),
                    ts_event_ns=int(record.ts_event),
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

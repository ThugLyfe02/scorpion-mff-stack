from __future__ import annotations

import statistics
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from .broker import Quote
from .fastpath import QuoteCacheStatus, QuoteLookup


class QuoteConsensusStatus(StrEnum):
    CONSENSUS = "CONSENSUS"
    INSUFFICIENT_PROVIDERS = "INSUFFICIENT_PROVIDERS"
    PROVIDER_DISAGREEMENT = "PROVIDER_DISAGREEMENT"
    CROSSED_CONSENSUS = "CROSSED_CONSENSUS"
    NO_FRESH_QUOTES = "NO_FRESH_QUOTES"


@dataclass(frozen=True, slots=True)
class ProviderQuote:
    provider: str
    contract_key: str
    quote: Quote


@dataclass(frozen=True, slots=True)
class QuoteConsensus:
    status: QuoteConsensusStatus
    quote: Quote | None
    providers_seen: int
    providers_used: tuple[str, ...]
    midpoint_dispersion: float
    reason: str


class ConsensusQuoteCache:
    """Thread-safe multi-provider quote cache with robust median aggregation.

    This is a market-data validation surface, not a broker. A fastpath can consume it through
    the same ``lookup``/``get`` contract as the simple quote cache. A rejected consensus
    retains its diagnostic reason and cannot produce a usable execution-preparation quote.
    """

    def __init__(
        self,
        *,
        minimum_providers: int = 2,
        maximum_midpoint_dispersion: float = 0.03,
    ) -> None:
        if minimum_providers <= 0:
            raise ValueError("minimum_providers must be positive")
        if not 0.0 <= maximum_midpoint_dispersion <= 1.0:
            raise ValueError("maximum_midpoint_dispersion must be between 0 and 1")
        self.minimum_providers = minimum_providers
        self.maximum_midpoint_dispersion = maximum_midpoint_dispersion
        self._lock = threading.RLock()
        self._quotes: dict[str, dict[str, Quote]] = {}

    def update(
        self,
        provider: str,
        contract_key: str,
        *,
        bid: Decimal,
        ask: Decimal,
        observed_ts_utc: datetime,
    ) -> None:
        if not provider.strip():
            raise ValueError("provider is required")
        if not contract_key.strip():
            raise ValueError("contract_key is required")
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError("invalid quote")
        quote = Quote(bid, ask, observed_ts_utc.astimezone(UTC))
        with self._lock:
            self._quotes.setdefault(contract_key, {})[provider] = quote

    def assess(
        self,
        contract_key: str,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(seconds=1),
    ) -> QuoteConsensus:
        if max_age < timedelta(0):
            raise ValueError("max_age cannot be negative")
        now = (now or datetime.now(UTC)).astimezone(UTC)
        with self._lock:
            snapshot = dict(self._quotes.get(contract_key, {}))
        if not snapshot:
            return QuoteConsensus(
                QuoteConsensusStatus.NO_FRESH_QUOTES,
                None,
                0,
                (),
                0.0,
                "no provider has published this contract",
            )

        fresh: list[tuple[str, Quote]] = []
        for provider, quote in snapshot.items():
            age = now - quote.observed_ts_utc.astimezone(UTC)
            if timedelta(0) <= age <= max_age:
                fresh.append((provider, quote))
        if not fresh:
            return QuoteConsensus(
                QuoteConsensusStatus.NO_FRESH_QUOTES,
                None,
                len(snapshot),
                (),
                0.0,
                "all provider quotes are stale or from the future",
            )
        if len(fresh) < self.minimum_providers:
            return QuoteConsensus(
                QuoteConsensusStatus.INSUFFICIENT_PROVIDERS,
                None,
                len(snapshot),
                tuple(sorted(provider for provider, _ in fresh)),
                0.0,
                f"need {self.minimum_providers} fresh providers; found {len(fresh)}",
            )

        mids = [(quote.bid + quote.ask) / Decimal("2") for _, quote in fresh]
        median_mid = statistics.median(mids)
        if median_mid <= 0:
            return QuoteConsensus(
                QuoteConsensusStatus.PROVIDER_DISAGREEMENT,
                None,
                len(snapshot),
                tuple(sorted(provider for provider, _ in fresh)),
                1.0,
                "non-positive consensus midpoint",
            )
        dispersion_decimal = max(abs(mid - median_mid) / median_mid for mid in mids)
        dispersion = float(dispersion_decimal)
        if dispersion > self.maximum_midpoint_dispersion:
            return QuoteConsensus(
                QuoteConsensusStatus.PROVIDER_DISAGREEMENT,
                None,
                len(snapshot),
                tuple(sorted(provider for provider, _ in fresh)),
                dispersion,
                "fresh provider midpoints exceed configured disagreement tolerance",
            )

        bid = statistics.median([quote.bid for _, quote in fresh])
        ask = statistics.median([quote.ask for _, quote in fresh])
        if ask < bid:
            return QuoteConsensus(
                QuoteConsensusStatus.CROSSED_CONSENSUS,
                None,
                len(snapshot),
                tuple(sorted(provider for provider, _ in fresh)),
                dispersion,
                "median provider aggregation produced a crossed market",
            )
        observed = min(quote.observed_ts_utc.astimezone(UTC) for _, quote in fresh)
        return QuoteConsensus(
            QuoteConsensusStatus.CONSENSUS,
            Quote(bid, ask, observed),
            len(snapshot),
            tuple(sorted(provider for provider, _ in fresh)),
            dispersion,
            "robust median consensus across fresh providers",
        )

    def get(
        self,
        contract_key: str,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(seconds=1),
    ) -> Quote | None:
        return self.assess(contract_key, now=now, max_age=max_age).quote

    def lookup(
        self,
        contract_key: str,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(seconds=1),
    ) -> QuoteLookup:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        consensus = self.assess(contract_key, now=timestamp, max_age=max_age)
        if consensus.quote is not None:
            age_ms = (timestamp - consensus.quote.observed_ts_utc).total_seconds() * 1_000.0
            return QuoteLookup(QuoteCacheStatus.FRESH, consensus.quote, age_ms, consensus.reason)
        stale = (
            consensus.status is QuoteConsensusStatus.NO_FRESH_QUOTES
            and consensus.providers_seen > 0
        )
        return QuoteLookup(
            QuoteCacheStatus.STALE if stale else QuoteCacheStatus.MISSING,
            None,
            None,
            consensus.reason,
        )

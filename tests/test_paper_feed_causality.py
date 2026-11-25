"""The receive clock cannot make an old exchange quote fresh."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scorpion.broker import Quote, StaleQuote
from scorpion.databento_feed import DatabentoFeed, DatabentoLiveClient, FeedCaptureError, FeedMessage
from scorpion.fastpath import QuoteCache

WALL = 1_790_000_000_000_000_000
MS = 1_000_000


@dataclass
class Clock:
    wall: int = WALL
    mono: int = 100 * MS


class Client:
    def add_symbols(self, symbols: Sequence[str]) -> None:
        pass

    async def stream(self, symbols: Sequence[str], *, snapshot: bool) -> AsyncIterator[FeedMessage]:
        yield message(WALL)


def message(ts: int, bid: str = "10.00", ask: str = "10.02") -> FeedMessage:
    return FeedMessage("XYZ", Decimal(bid), Decimal(ask), None, ts)


def build() -> tuple[Clock, QuoteCache, DatabentoFeed]:
    clock = Clock()
    cache = QuoteCache(mono_ns=lambda: clock.mono)
    feed = DatabentoFeed(
        Client(), cache, ["XYZ"], wall_ns=lambda: clock.wall, mono_ns=lambda: clock.mono,
        max_disconnects=1,
    )
    return clock, cache, feed


def test_exchange_delay_counts_against_total_quote_age() -> None:
    clock, cache, feed = build()
    feed._apply(message(WALL - 2_900 * MS))
    assert isinstance(cache.fresh("XYZ", max_age_ms=3000), Quote)
    clock.mono += 101 * MS
    assert isinstance(cache.fresh("XYZ", max_age_ms=3000), StaleQuote)


def test_delayed_and_future_quotes_never_price() -> None:
    _, cache, feed = build()
    feed._apply(message(WALL - 3_001 * MS))
    feed._apply(message(WALL + 1))
    assert isinstance(cache.fresh("XYZ", max_age_ms=3000), StaleQuote)
    assert feed.stats.rejected_time == 2
    assert cache.last_message_mono_ns is None


def test_duplicate_and_reordered_quote_cannot_refresh_or_replace() -> None:
    clock, cache, feed = build()
    feed._apply(message(WALL))
    clock.wall += 100 * MS
    clock.mono += 100 * MS
    feed._apply(message(WALL))
    feed._apply(message(WALL - 1, "90", "91"))
    quote = cache.fresh("XYZ", max_age_ms=3000)
    assert isinstance(quote, Quote)
    assert quote.bid == Decimal("10")
    assert cache.age_ms("XYZ") == 100
    assert feed.stats.rejected_order == 2


def test_withdrawn_or_nonfinite_quote_invalidates_previous_price() -> None:
    clock, cache, feed = build()
    feed._apply(message(WALL))
    clock.wall += MS
    feed._apply(message(clock.wall, "NaN", "10.02"))
    assert isinstance(cache.fresh("XYZ", max_age_ms=3000), StaleQuote)
    assert feed.stats.rejected_quote == 1


def test_disconnect_invalidates_snapshot_and_cannot_be_replayed() -> None:
    _, cache, feed = build()
    asyncio.run(feed.run())
    assert isinstance(cache.fresh("XYZ", max_age_ms=3000), StaleQuote)
    feed._apply(message(WALL))
    assert isinstance(cache.fresh("XYZ", max_age_ms=3000), StaleQuote)
    assert feed.stats.rejected_order == 1


def test_monotonic_clock_regression_is_not_hidden_by_source_age() -> None:
    clock, cache, feed = build()
    feed._apply(message(WALL - 100 * MS))
    clock.mono -= MS
    assert isinstance(cache.fresh("XYZ", max_age_ms=3000), StaleQuote)


def test_conflicting_equal_timestamp_invalidates_until_strictly_newer_update() -> None:
    clock, cache, feed = build()
    feed._apply(message(WALL))
    feed._apply(message(WALL, "0", "10.02"))
    assert isinstance(cache.fresh("XYZ", max_age_ms=3000), StaleQuote)
    assert feed.stats.timestamp_conflicts == 1
    feed._apply(message(WALL))
    assert isinstance(cache.fresh("XYZ", max_age_ms=3000), StaleQuote)
    clock.wall += MS
    clock.mono += MS
    feed._apply(message(clock.wall))
    assert isinstance(cache.fresh("XYZ", max_age_ms=3000), Quote)


def test_unrequested_symbol_cannot_refresh_feed_or_seed_cache() -> None:
    _, cache, feed = build()
    feed._apply(FeedMessage("OTHER", Decimal("2"), Decimal("3"), None, WALL))
    assert cache.last_message_mono_ns is None
    assert feed.stats.rejected_symbol == 1
    assert isinstance(cache.fresh("OTHER", max_age_ms=3000), StaleQuote)
    feed.add_symbol("OTHER")
    feed._apply(FeedMessage("OTHER", Decimal("2"), Decimal("3"), None, WALL))
    assert isinstance(cache.fresh("OTHER", max_age_ms=3000), Quote)


def test_failed_subscription_is_retryable() -> None:
    class FailingClient(Client):
        calls = 0

        def add_symbols(self, symbols: Sequence[str]) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("fixture subscription failure")

    import pytest

    clock = Clock()
    client = FailingClient()
    cache = QuoteCache(mono_ns=lambda: clock.mono)
    feed = DatabentoFeed(
        client, cache, ["XYZ"], wall_ns=lambda: clock.wall, mono_ns=lambda: clock.mono,
    )
    with pytest.raises(RuntimeError):
        feed.add_symbol("OTHER")
    feed.add_symbol("OTHER")
    assert client.calls == 2


def test_clock_regression_is_not_a_healthy_symbol_age() -> None:
    clock, cache, feed = build()
    feed._apply(message(WALL - 200 * MS))
    clock.mono -= MS
    assert cache.age_ms("XYZ") is None


def test_observation_sink_binds_depth_and_fails_closed() -> None:
    clock, cache, _ = build()
    records: list[dict[str, object]] = []
    feed = DatabentoFeed(
        Client(), cache, ["XYZ"], wall_ns=lambda: clock.wall, mono_ns=lambda: clock.mono,
        on_observation=records.append,
    )
    row = FeedMessage("XYZ", Decimal("10"), Decimal("10.02"), None, WALL, 12, 8, 1, 2, 3)
    feed._apply(row)
    feed._apply(row)
    assert len(records) == 1
    assert records[0]["ask_size"] == 8 and records[0]["provider_sequence"] == 3
    feed._apply(FeedMessage("XYZ", Decimal("10"), Decimal("10.02"), None, WALL, 12, 9))
    assert records[-1]["reason"] == "timestamp_conflict"

    def broken(_: dict[str, object]) -> None:
        raise OSError("fixture failure")

    failing = DatabentoFeed(
        Client(), QuoteCache(), ["XYZ"], wall_ns=lambda: WALL, on_observation=broken,
        max_disconnects=1,
    )
    with pytest.raises(FeedCaptureError):
        asyncio.run(failing.run())


def test_live_adapter_mbp_never_requests_mbo_snapshot_and_never_prices_order_action(
    monkeypatch,
) -> None:
    subscriptions = []
    class FakeLive:
        symbology_map = {2: "XYZ"}

        def __init__(self, **kwargs):
            assert kwargs["slow_reader_behavior"] == "warn"

        def subscribe(self, **kwargs):
            subscriptions.append(kwargs)

        async def __aiter__(self):
            yield SimpleNamespace(
                instrument_id=2, publisher_id=1, sequence=8, ts_event=WALL, action="A",
                price=90_000_000_000, levels=[SimpleNamespace(
                    bid_px=10_000_000_000, ask_px=10_020_000_000, bid_sz=20, ask_sz=12,
                )],
            )
            yield type("ErrorMsg", (), {})()

        def stop(self):
            pass

    monkeypatch.setenv("DATABENTO_API_KEY", "synthetic-test-key")
    monkeypatch.setitem(sys.modules, "databento", SimpleNamespace(Live=FakeLive))

    async def consume():
        adapter = DatabentoLiveClient()
        stream = adapter.stream(["XYZ"], snapshot=True)
        row = await anext(stream)
        assert row.last is None and row.ask_size == 12
        adapter.add_symbols(["OTHER"])
        with pytest.raises(RuntimeError, match="provider_error"):
            await anext(stream)

    asyncio.run(consume())
    assert subscriptions and all(row["snapshot"] is False for row in subscriptions)


def test_first_usable_quote_readiness_cannot_be_backfilled_or_refreshed() -> None:
    clock, _, feed = build()
    feed._apply(message(WALL - 3_001 * MS))
    feed._apply(message(WALL + 1))
    feed._apply(message(WALL, "NaN", "10.02"))
    assert feed.stats.first_usable_recv_wall_ns is None
    clock.wall += MS
    clock.mono += MS
    feed._apply(message(clock.wall))
    first = clock.wall
    assert feed.stats.first_usable_recv_wall_ns == first
    clock.wall += MS
    clock.mono += MS
    feed._apply(message(clock.wall))
    assert feed.stats.first_usable_recv_wall_ns == first

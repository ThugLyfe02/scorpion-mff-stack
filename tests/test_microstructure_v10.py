import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.history_archive import ArchivedDiscordMessage
from scorpion.microstructure import (
    AggressorSide,
    FillCertainty,
    MarketEventKind,
    OptionMarketEvent,
    OptionMicrostructureTape,
    ns_from_datetime,
)
from scorpion.microstructure_forensics import (
    MicroForensicStatus,
    MicrostructureExecutionProfile,
    run_microstructure_forensics,
)
from scorpion.microstructure_live import (
    LiveShadowRecorder,
    ShadowExecutionIntent,
    ShadowIntentSide,
    replay_market_events_wall_clock,
)
from scorpion.opra_bridge import contract_key_from_occ_symbol, normalize_databento_opra_row
from scorpion.quote_tape import HistoricalQuote, HistoricalQuoteTape

GUILD = "912747256736800838"
CHANNEL = "1448448931116748993"
AUTHOR = "author"
CONTRACT = "AAPL|CALL|200|2026-09-08"
BASE = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


def _quote(
    offset_ms: int,
    *,
    bid: str,
    ask: str,
    bid_size: int | None = 10,
    ask_size: int | None = 10,
    recv_extra_ms: int = 1,
    sequence: int = 1,
) -> OptionMarketEvent:
    event_ts = BASE + timedelta(milliseconds=offset_ms)
    recv_ts = event_ts + timedelta(milliseconds=recv_extra_ms)
    return OptionMarketEvent(
        contract_key=CONTRACT,
        kind=MarketEventKind.QUOTE,
        ts_event_ns=ns_from_datetime(event_ts),
        ts_recv_ns=ns_from_datetime(recv_ts),
        publisher_id=30,
        sequence=sequence,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=bid_size,
        ask_size=ask_size,
        bid_publisher_id=36,
        ask_publisher_id=32,
    )


def _trade(offset_ms: int, *, price: str, size: int, sequence: int = 1) -> OptionMarketEvent:
    event_ts = BASE + timedelta(milliseconds=offset_ms)
    return OptionMarketEvent(
        contract_key=CONTRACT,
        kind=MarketEventKind.TRADE,
        ts_event_ns=ns_from_datetime(event_ts),
        ts_recv_ns=ns_from_datetime(event_ts + timedelta(milliseconds=1)),
        publisher_id=32,
        sequence=sequence,
        trade_price=Decimal(price),
        trade_size=size,
        aggressor_side=AggressorSide.SELL,
    )


def _message(message_id: str, offset_s: int, content: str) -> ArchivedDiscordMessage:
    return ArchivedDiscordMessage(
        message_id=message_id,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=AUTHOR,
        source_ts_utc=BASE + timedelta(seconds=offset_s),
        content=content,
    )


def test_quote_tape_decision_lookup_never_uses_future_quote():
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(CONTRACT, BASE, Decimal("0.95"), Decimal("1.00")),
            HistoricalQuote(
                CONTRACT,
                BASE + timedelta(milliseconds=300),
                Decimal("1.25"),
                Decimal("1.30"),
            ),
        )
    )
    decision = BASE + timedelta(milliseconds=250)
    quote = tape.at_or_before(CONTRACT, decision, max_age=timedelta(seconds=1))
    assert quote is not None
    assert quote.ask == Decimal("1.00")


def test_decision_quote_uses_provider_receive_clock_not_future_capture():
    early = _quote(0, bid="0.95", ask="1.00", recv_extra_ms=10, sequence=1)
    late = _quote(200, bid="1.25", ask="1.30", recv_extra_ms=100, sequence=2)
    tape = OptionMicrostructureTape((early, late))
    decision = BASE + timedelta(milliseconds=250)
    visible = tape.decision_quote(CONTRACT, decision, max_age=timedelta(seconds=1))
    assert visible is not None
    assert visible.quote.ask == Decimal("1.00")
    assert visible.quote.sequence == 1


def test_marketable_limit_fill_is_certified_only_to_displayed_depth():
    tape = OptionMicrostructureTape((_quote(0, bid="0.99", ask="1.00", ask_size=2),))
    fill = tape.simulate_limit_buy(
        CONTRACT,
        order_arrival_ts_utc=BASE + timedelta(milliseconds=10),
        quantity=5,
        limit_price=Decimal("1.05"),
    )
    assert fill.certainty is FillCertainty.AGGRESSIVE_DISPLAYED_PARTIAL
    assert fill.lower_bound_quantity == 2
    assert fill.upper_bound_quantity == 2
    assert fill.conservative_complete is False


def test_resting_limit_never_becomes_certain_from_l1_trade_prints():
    tape = OptionMicrostructureTape(
        (
            _quote(0, bid="1.00", ask="1.20", sequence=1),
            _trade(200, price="1.05", size=3, sequence=2),
        )
    )
    fill = tape.simulate_limit_buy(
        CONTRACT,
        order_arrival_ts_utc=BASE + timedelta(milliseconds=10),
        quantity=3,
        limit_price=Decimal("1.10"),
        max_wait=timedelta(seconds=1),
    )
    assert fill.certainty is FillCertainty.RESTING_QUEUE_UNKNOWN
    assert fill.lower_bound_quantity == 0
    assert fill.upper_bound_quantity == 3


def test_aggressive_exit_is_capped_by_displayed_bid_depth():
    tape = OptionMicrostructureTape((_quote(0, bid="1.20", ask="1.22", bid_size=2),))
    fill = tape.simulate_aggressive_sell(
        CONTRACT,
        order_arrival_ts_utc=BASE + timedelta(milliseconds=10),
        quantity=5,
    )
    assert fill.certainty is FillCertainty.AGGRESSIVE_DISPLAYED_PARTIAL
    assert fill.lower_bound_quantity == 2
    assert fill.upper_bound_quantity == 2


def test_microstructure_forensics_completes_only_certified_entry_and_exit():
    exit_base = BASE + timedelta(seconds=60)
    entry_quote = _quote(100, bid="0.98", ask="1.00", sequence=1)
    exit_event_ts = exit_base + timedelta(milliseconds=100)
    exit_quote = OptionMarketEvent(
        contract_key=CONTRACT,
        kind=MarketEventKind.QUOTE,
        ts_event_ns=ns_from_datetime(exit_event_ts),
        ts_recv_ns=ns_from_datetime(exit_event_ts + timedelta(milliseconds=1)),
        publisher_id=30,
        sequence=2,
        bid=Decimal("1.20"),
        ask=Decimal("1.22"),
        bid_size=10,
        ask_size=10,
    )
    tape = OptionMicrostructureTape((entry_quote, exit_quote))
    legs, completed = run_microstructure_forensics(
        (
            _message("entry", 0, "AAPL 200C TODAY @ 1.00"),
            _message("exit", 60, "closing runners"),
        ),
        tape,
        allowed_author_ids=frozenset({AUTHOR}),
        profile=MicrostructureExecutionProfile(
            decision_latency=timedelta(milliseconds=250),
            decision_quote_max_age=timedelta(seconds=1),
            market_quote_max_age=timedelta(seconds=1),
        ),
    )
    assert any(leg.status is MicroForensicStatus.CERTIFIED_FILL for leg in legs)
    assert len(completed) == 1
    assert completed[0].pnl == Decimal("20.00")
    assert completed[0].depth_evidence_complete is True


def test_live_shadow_reuses_same_microstructure_fill_engine():
    recorder = LiveShadowRecorder()
    recorder.on_event(_quote(0, bid="0.99", ask="1.00", ask_size=5))
    intent = ShadowExecutionIntent(
        intent_id="intent-1",
        contract_key=CONTRACT,
        side=ShadowIntentSide.BUY_LIMIT,
        quantity=2,
        limit_price=Decimal("1.05"),
        order_arrival_ts_utc=BASE + timedelta(milliseconds=10),
    )
    result = recorder.evaluate(intent)
    assert result.fill.certainty is FillCertainty.AGGRESSIVE_DISPLAYED_COMPLETE
    assert result.fill.lower_bound_quantity == 2


def test_wall_clock_replay_preserves_provider_receive_order_without_sleep():
    first = _quote(0, bid="0.99", ask="1.00", sequence=1)
    second = _trade(10, price="1.00", size=1, sequence=2)
    observed: list[int] = []

    async def sink(event: OptionMarketEvent) -> None:
        observed.append(event.sequence)

    asyncio.run(replay_market_events_wall_clock((second, first), sink, speed=0))
    assert observed == [1, 2]


def test_opra_bridge_parses_occ_and_emits_quote_plus_trade():
    assert contract_key_from_occ_symbol("AAPL  260908C00200000") == CONTRACT
    events = normalize_databento_opra_row(
        {
            "symbol": "AAPL  260908C00200000",
            "ts_recv": str(ns_from_datetime(BASE + timedelta(milliseconds=2))),
            "ts_event": str(ns_from_datetime(BASE + timedelta(milliseconds=1))),
            "publisher_id": "30",
            "sequence": "7",
            "action": "T",
            "side": "A",
            "price": "1050000000",
            "size": "3",
            "bid_px_00": "1000000000",
            "ask_px_00": "1100000000",
            "bid_sz_00": "4",
            "ask_sz_00": "5",
            "bid_pb_00": "36",
            "ask_pb_00": "32",
        }
    )
    assert len(events) == 2
    quote = next(event for event in events if event.kind is MarketEventKind.QUOTE)
    trade = next(event for event in events if event.kind is MarketEventKind.TRADE)
    assert quote.bid == Decimal("1.000000000")
    assert quote.ask == Decimal("1.100000000")
    assert quote.ask_size == 5
    assert trade.trade_price == Decimal("1.050000000")
    assert trade.trade_size == 3
    assert trade.aggressor_side is AggressorSide.SELL

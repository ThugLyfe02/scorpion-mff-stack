from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.history_archive import ArchivedDiscordMessage
from scorpion.microstructure import MarketEventKind, OptionMarketEvent, OptionMicrostructureTape
from scorpion.microstructure_forensics import MicroForensicLeg, MicroForensicStatus
from scorpion.microstructure_windows import build_acquisition_plan, verify_acquisition_warmup
from scorpion.regime_stability import RegimeStabilityStatus, evaluate_regime_stability

GUILD = "912747256736800838"
CHANNEL = "968352649437126676"
AUTHOR = "author"
CONTRACT = "AAPL|CALL|200|2026-09-08"
BASE = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


def _message(message_id: str, offset_s: int, content: str) -> ArchivedDiscordMessage:
    return ArchivedDiscordMessage(
        message_id=message_id,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=AUTHOR,
        source_ts_utc=BASE + timedelta(seconds=offset_s),
        content=content,
    )


def _entry_leg(
    event_id: str,
    *,
    source: datetime,
    latency_ms: int,
    quote_age_ms: int,
    bid: str,
    ask: str,
) -> MicroForensicLeg:
    decision = source + timedelta(milliseconds=latency_ms)
    decision_ns = int(decision.timestamp() * 1_000_000_000)
    return MicroForensicLeg(
        event_id=event_id,
        message_id=event_id,
        event_kind="ENTRY",  # type: ignore[arg-type]
        contract_key=CONTRACT,
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        status=MicroForensicStatus.CERTIFIED_FILL,
        source_ts_utc=source,
        decision_ts_utc=decision,
        decision_quote_event_ns=decision_ns - quote_age_ms * 1_000_000,
        decision_quote_recv_ns=decision_ns - quote_age_ms * 1_000_000,
        decision_quote_bid=Decimal(bid),
        decision_quote_ask=Decimal(ask),
        limit_price=Decimal(ask),
        order_arrival_ts_utc=decision,
        requested_quantity=1,
        conservative_fill_quantity=1,
        possible_fill_quantity=1,
        fill_price=Decimal(ask),
        fill_certainty=None,
        fill_evidence_event_ns=decision_ns,
        fill_evidence_publisher_id=30,
    )


def _trade(event_id: str, source: datetime, value: str) -> CompletedTrade:
    premium = Decimal("100")
    return_fraction = Decimal(value)
    pnl = premium * return_fraction
    return CompletedTrade(
        entry_event_id=event_id,
        contract_key=CONTRACT,
        channel_id=CHANNEL,
        author_id=AUTHOR,
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=source,
        closed_ts_utc=source + timedelta(minutes=2),
        initial_quantity=1,
        add_count=0,
        trim_count=0,
        gross_premium_in=premium,
        gross_proceeds=premium + pnl,
        pnl=pnl,
        return_fraction=return_fraction,
        holding_seconds=120.0,
        depth_evidence_complete=True,
    )


def test_acquisition_plan_separates_analysis_window_from_quote_state_warmup():
    messages = (
        _message("1", 0, "AAPL 200C TODAY @ 1.00"),
        _message("2", 5, "added @ 0.95"),
    )
    plan = build_acquisition_plan(
        messages,
        allowed_author_ids=frozenset({AUTHOR}),
        pre_context=timedelta(seconds=2),
        post_context=timedelta(seconds=10),
        quote_warmup=timedelta(seconds=60),
    )
    assert len(plan.windows) == 1
    assert plan.total_window_seconds == 17.0
    assert plan.total_query_seconds == 77.0
    assert plan.windows[0].warmup_seconds == 60.0

    analysis_start = plan.windows[0].starts_ts_utc
    quote_ts = analysis_start - timedelta(seconds=30)
    quote_ns = int(quote_ts.timestamp() * 1_000_000_000)
    tape = OptionMicrostructureTape(
        (
            OptionMarketEvent(
                contract_key=CONTRACT,
                kind=MarketEventKind.QUOTE,
                ts_event_ns=quote_ns,
                ts_recv_ns=quote_ns,
                publisher_id=30,
                sequence=1,
                bid=Decimal("1.00"),
                ask=Decimal("1.05"),
                bid_size=10,
                ask_size=10,
            ),
        )
    )
    coverage = verify_acquisition_warmup(plan, tape)
    assert coverage.complete is True
    assert coverage.coverage_rate == 1.0


def test_regime_stability_passes_only_when_edge_survives_multiple_market_states():
    trades: list[CompletedTrade] = []
    legs: list[MicroForensicLeg] = []
    for index in range(40):
        event_id = f"event-{index}"
        if index % 2 == 0:
            source = datetime(2026, 9, 8, 13, 45, tzinfo=UTC)
            leg = _entry_leg(
                event_id,
                source=source,
                latency_ms=50,
                quote_age_ms=10,
                bid="1.00",
                ask="1.02",
            )
            value = "0.08"
        else:
            source = datetime(2026, 9, 8, 17, 0, tzinfo=UTC)
            leg = _entry_leg(
                event_id,
                source=source,
                latency_ms=250,
                quote_age_ms=100,
                bid="1.00",
                ask="1.06",
            )
            value = "0.04"
        legs.append(leg)
        trades.append(_trade(event_id, source, value))

    report = evaluate_regime_stability(tuple(trades), tuple(legs))
    assert report.status is RegimeStabilityStatus.PASS
    assert report.passed is True
    assert all(axis.status is RegimeStabilityStatus.PASS for axis in report.axes)


def test_regime_stability_rejects_evidence_concentrated_in_one_bucket():
    trades: list[CompletedTrade] = []
    legs: list[MicroForensicLeg] = []
    for index in range(30):
        event_id = f"concentrated-{index}"
        dominant = index < 26
        source = (
            datetime(2026, 9, 8, 13, 45, tzinfo=UTC)
            if dominant
            else datetime(2026, 9, 8, 17, 0, tzinfo=UTC)
        )
        leg = _entry_leg(
            event_id,
            source=source,
            latency_ms=50 if dominant else 700,
            quote_age_ms=10 if dominant else 500,
            bid="1.00",
            ask="1.02" if dominant else "1.20",
        )
        legs.append(leg)
        trades.append(_trade(event_id, source, "0.08" if dominant else "-0.10"))

    report = evaluate_regime_stability(tuple(trades), tuple(legs))
    assert report.passed is False
    assert report.status is RegimeStabilityStatus.FAIL
    assert report.failures

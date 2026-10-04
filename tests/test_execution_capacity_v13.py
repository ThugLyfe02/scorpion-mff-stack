from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.contract_terms import ContractTerms, ContractTermsRegistry
from scorpion.domain import EventKind
from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.liquidity_capacity import LiquidityCapacityPolicy, evaluate_liquidity_capacity
from scorpion.microstructure import MarketEventKind, OptionMarketEvent, OptionMicrostructureTape
from scorpion.microstructure_forensics import (
    MicroForensicLeg,
    MicroForensicStatus,
    MicrostructureForensicsReport,
)
from scorpion.tick_validation import round_buy_limit_down, validate_buy_limit_ticks

BASE = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


def _terms(increment: str | None) -> ContractTermsRegistry:
    return ContractTermsRegistry(
        (
            ContractTerms(
                contract_key="AAPL|CALL|200|2026-09-18",
                valid_from_ns=0,
                valid_to_ns=None,
                multiplier=Decimal("100"),
                deliverable="100 shares AAPL",
                adjusted=False,
                source="definition",
                min_price_increment=(Decimal(increment) if increment is not None else None),
            ),
        )
    )


def _leg(
    event_id: str,
    contract_key: str,
    kind: EventKind,
    source: datetime,
    fill_ns: int,
    *,
    requested: int = 10,
) -> MicroForensicLeg:
    return MicroForensicLeg(
        event_id=event_id,
        message_id=event_id,
        event_kind=kind,
        contract_key=contract_key,
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        status=MicroForensicStatus.CERTIFIED_FILL,
        source_ts_utc=source,
        decision_ts_utc=source,
        decision_quote_event_ns=fill_ns,
        decision_quote_recv_ns=fill_ns,
        decision_quote_bid=Decimal("1.00"),
        decision_quote_ask=Decimal("1.05"),
        limit_price=Decimal("1.1615") if kind is EventKind.ENTRY else None,
        order_arrival_ts_utc=source,
        requested_quantity=requested,
        conservative_fill_quantity=requested,
        possible_fill_quantity=requested,
        fill_price=Decimal("1.05"),
        fill_certainty=None,
        fill_evidence_event_ns=fill_ns,
        fill_evidence_publisher_id=30,
    )


def test_contract_execution_readiness_requires_price_increment_and_tick_alignment():
    key = "AAPL|CALL|200|2026-09-18"
    without_increment = _terms(None).coverage(((key, 1),))
    assert without_increment.standard_only is True
    assert without_increment.execution_ready is False
    assert without_increment.missing_price_increment == (key,)

    registry = _terms("0.05")
    with_increment = registry.coverage(((key, 1),))
    assert with_increment.execution_ready is True
    leg = _leg("entry", key, EventKind.ENTRY, BASE, int(BASE.timestamp() * 1_000_000_000))
    alignment = validate_buy_limit_ticks((leg,), registry)
    assert alignment.all_aligned is False
    assert alignment.misaligned_limits == 1
    assert alignment.issues[0].rounded_down_limit == Decimal("1.15")
    assert round_buy_limit_down(Decimal("1.1615"), Decimal("0.05")) == Decimal("1.15")


def test_liquidity_capacity_exposes_clip_size_that_breaks_under_depth_haircut():
    trades: list[CompletedTrade] = []
    legs: list[MicroForensicLeg] = []
    events: list[OptionMarketEvent] = []
    for index in range(20):
        opened = BASE + timedelta(days=index)
        closed = opened + timedelta(minutes=1)
        contract = f"T{index}|CALL|100|2026-10-16"
        entry_ns = int(opened.timestamp() * 1_000_000_000)
        exit_ns = int(closed.timestamp() * 1_000_000_000)
        entry_id = f"entry-{index}"
        exit_id = f"exit-{index}"
        legs.extend(
            (
                _leg(entry_id, contract, EventKind.ENTRY, opened, entry_ns),
                _leg(exit_id, contract, EventKind.EXIT, closed, exit_ns),
            )
        )
        events.extend(
            (
                OptionMarketEvent(
                    contract_key=contract,
                    kind=MarketEventKind.QUOTE,
                    ts_event_ns=entry_ns,
                    ts_recv_ns=entry_ns,
                    publisher_id=30,
                    sequence=index * 2,
                    bid=Decimal("1.00"),
                    ask=Decimal("1.05"),
                    bid_size=100,
                    ask_size=100,
                ),
                OptionMarketEvent(
                    contract_key=contract,
                    kind=MarketEventKind.QUOTE,
                    ts_event_ns=exit_ns,
                    ts_recv_ns=exit_ns,
                    publisher_id=30,
                    sequence=index * 2 + 1,
                    bid=Decimal("1.15"),
                    ask=Decimal("1.20"),
                    bid_size=100,
                    ask_size=100,
                ),
            )
        )
        trades.append(
            CompletedTrade(
                entry_event_id=entry_id,
                contract_key=contract,
                channel_id="channel",
                author_id="author",
                bucket=StrategyBucket.CORE_SINGLE_NAME,
                opened_ts_utc=opened,
                closed_ts_utc=closed,
                initial_quantity=10,
                add_count=0,
                trim_count=0,
                gross_premium_in=Decimal("1050"),
                gross_proceeds=Decimal("1150"),
                pnl=Decimal("100"),
                return_fraction=Decimal("0.0952380952"),
                holding_seconds=60.0,
                depth_evidence_complete=True,
            )
        )
    report = MicrostructureForensicsReport(
        messages_seen=40,
        completeness=(),
        legs=tuple(legs),
        completed_trades=tuple(trades),
        policy_fingerprint="policy",
        certified_actionable_legs=40,
        uncertain_actionable_legs=0,
    )
    capacity = evaluate_liquidity_capacity(
        report,
        OptionMicrostructureTape(tuple(events)),
        policy=LiquidityCapacityPolicy(
            clip_multipliers=(1.0, 2.0, 3.0),
            displayed_depth_haircuts=(0.25, 0.50, 1.0),
            minimum_supported_trades=20,
            minimum_lifecycle_coverage=0.80,
        ),
    )
    assert capacity.robust is True
    assert capacity.max_robust_clip_multiplier == 2.0
    point_3x = next(item for item in capacity.frontier if item.clip_multiplier == 3.0)
    assert point_3x.robust is False
    assert point_3x.worst_lifecycle_coverage == 0.0

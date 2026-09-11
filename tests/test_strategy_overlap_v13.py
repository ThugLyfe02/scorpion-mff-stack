from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.strategy_overlap import (
    analyze_strategy_overlap,
    deduplicate_strategy_universe,
)

BASE = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
CHANNEL = "968352649437126676"
AUTHOR = "author"


def _trade(event_id: str, ticker: str, offset: int) -> CompletedTrade:
    opened = BASE + timedelta(minutes=offset)
    return CompletedTrade(
        entry_event_id=event_id,
        contract_key=f"{ticker}|CALL|100|2026-09-18",
        channel_id=CHANNEL,
        author_id=AUTHOR,
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=opened,
        closed_ts_utc=opened + timedelta(minutes=2),
        initial_quantity=1,
        add_count=0,
        trim_count=0,
        gross_premium_in=Decimal("100"),
        gross_proceeds=Decimal("110"),
        pnl=Decimal("10"),
        return_fraction=Decimal("0.10"),
        holding_seconds=120.0,
        depth_evidence_complete=True,
    )


def test_nested_strategy_slices_collapse_into_one_effective_hypothesis():
    a = _trade("a", "AAPL", 0)
    b = _trade("b", "AAPL", 1)
    c = _trade("c", "AAPL", 2)
    d = _trade("d", "NVDA", 3)
    e = _trade("e", "MSFT", 4)
    f = _trade("f", "MSFT", 5)
    segments = {
        "all": (a, b, c, d),
        "channel:king": (a, b, c),
        "ticker:MSFT": (e, f),
    }
    report = analyze_strategy_overlap(segments)
    assert report.strategies == 3
    assert report.effective_hypotheses == 2
    assert report.duplicate_strategies == 1
    equivalent = [pair for pair in report.pairs if pair.equivalent]
    assert len(equivalent) == 1
    assert {equivalent[0].left, equivalent[0].right} == {"all", "channel:king"}


def test_deduplicated_universe_prefers_specific_nested_representative_deterministically():
    a = _trade("a", "AAPL", 0)
    b = _trade("b", "AAPL", 1)
    c = _trade("c", "AAPL", 2)
    d = _trade("d", "NVDA", 3)
    e = _trade("e", "MSFT", 4)
    segments = {
        "all": (a, b, c, d),
        "channel:king": (a, b, c),
        "ticker:MSFT": (e,),
    }
    universe, report = deduplicate_strategy_universe(segments)
    assert report.effective_hypotheses == 2
    assert set(universe) == {"channel:king", "ticker:MSFT"}
    repeated, repeated_report = deduplicate_strategy_universe(dict(reversed(tuple(segments.items()))))
    assert tuple(universe) == tuple(repeated)
    assert report.clusters == repeated_report.clusters


def test_large_umbrella_segment_cannot_bridge_disjoint_children_into_one_cluster():
    aapl = tuple(_trade(f"aapl-{index}", "AAPL", index) for index in range(10))
    nvda = tuple(_trade(f"nvda-{index}", "NVDA", 20 + index) for index in range(10))
    segments = {
        "all": aapl + nvda,
        "ticker:AAPL": aapl,
        "ticker:NVDA": nvda,
    }
    universe, report = deduplicate_strategy_universe(segments)
    assert report.effective_hypotheses == 3
    assert set(universe) == {"all", "ticker:AAPL", "ticker:NVDA"}
    child_pair = next(
        pair
        for pair in report.pairs
        if {pair.left, pair.right} == {"ticker:AAPL", "ticker:NVDA"}
    )
    assert child_pair.shared_trades == 0
    assert child_pair.equivalent is False
    umbrella_pairs = [
        pair for pair in report.pairs if "all" in {pair.left, pair.right}
    ]
    assert all(pair.containment == 1.0 for pair in umbrella_pairs)
    assert all(pair.size_ratio == 0.5 for pair in umbrella_pairs)
    assert all(pair.equivalent is False for pair in umbrella_pairs)

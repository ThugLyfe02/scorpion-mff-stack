from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.tail_dependence import TailDependencePolicy, evaluate_tail_dependence

BASE = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
CHANNEL = "968352649437126676"


def _trade(day_index: int, ticker: str, value: float) -> CompletedTrade:
    opened = BASE + timedelta(days=day_index)
    gross = Decimal("100")
    pnl = gross * Decimal(str(value))
    return CompletedTrade(
        entry_event_id=f"{ticker}-{day_index}",
        contract_key=f"{ticker}|CALL|200|2026-12-18",
        channel_id=CHANNEL,
        author_id="author",
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=opened,
        closed_ts_utc=opened + timedelta(minutes=5),
        initial_quantity=1,
        add_count=0,
        trim_count=0,
        gross_premium_in=gross,
        gross_proceeds=gross + pnl,
        pnl=pnl,
        return_fraction=Decimal(str(value)),
        holding_seconds=300.0,
        depth_evidence_complete=True,
    )


def test_tail_dependence_clusters_distinct_tickers_with_shared_failure_days():
    trades: list[CompletedTrade] = []
    shared_bad_days = set(range(0, 40, 5))
    independent_bad_days = set(range(2, 40, 5))
    for day in range(40):
        shared_return = -0.20 if day in shared_bad_days else 0.04
        independent_return = -0.20 if day in independent_bad_days else 0.04
        trades.extend(
            (
                _trade(day, "AAPL", shared_return),
                _trade(day, "NVDA", shared_return),
                _trade(day, "MSFT", independent_return),
            )
        )
    report = evaluate_tail_dependence(
        tuple(trades),
        policy=TailDependencePolicy(
            tail_fraction=0.20,
            minimum_overlap_days=30,
            minimum_tail_events=5,
            cluster_dependence_threshold=0.60,
        ),
    )
    linked = {
        frozenset((item.left, item.right))
        for item in report.pairs
        if item.cluster_link
    }
    assert frozenset(("AAPL", "NVDA")) in linked
    assert frozenset(("AAPL", "MSFT")) not in linked
    assert frozenset(("NVDA", "MSFT")) not in linked
    clusters = [set(item.members) for item in report.clusters]
    assert {"AAPL", "NVDA"} in clusters
    assert {"MSFT"} in clusters
    assert report.maximum_cluster_size == 2

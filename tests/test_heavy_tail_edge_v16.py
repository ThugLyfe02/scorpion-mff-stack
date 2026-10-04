from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.heavy_tail_edge import HeavyTailPolicy, HeavyTailStatus, evaluate_heavy_tail_edge

BASE = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
CHANNEL = "968352649437126676"


def _trade(index: int, value: float) -> CompletedTrade:
    opened = BASE + timedelta(days=index)
    gross = Decimal("100")
    pnl = gross * Decimal(str(value))
    return CompletedTrade(
        entry_event_id=f"e-{index}",
        contract_key="AAPL|CALL|200|2026-12-18",
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


def test_heavy_tail_gate_rejects_edge_carried_by_hero_trades():
    values = [-0.01] * 48 + [1.00, 1.00]
    trades = tuple(_trade(index, value) for index, value in enumerate(values))
    report = evaluate_heavy_tail_edge(
        "hero-dependent",
        trades,
        policy=HeavyTailPolicy(
            minimum_samples=30,
            blocks=5,
            minimum_block_samples=5,
            bootstrap_trials=500,
            minimum_positive_block_ratio=0.60,
        ),
    )
    assert report.ordinary_mean > 0
    assert report.status is HeavyTailStatus.FAILED
    assert report.median_of_means < 0
    assert report.bootstrap_lower_bound < 0
    assert report.ordinary_minus_robust > 0.03


def test_heavy_tail_gate_passes_consistent_positive_blocks():
    values = [0.01 + (index % 5) * 0.001 for index in range(50)]
    trades = tuple(_trade(index, value) for index, value in enumerate(values))
    report = evaluate_heavy_tail_edge(
        "stable",
        trades,
        policy=HeavyTailPolicy(
            minimum_samples=30,
            blocks=5,
            minimum_block_samples=5,
            bootstrap_trials=500,
            minimum_positive_block_ratio=0.80,
        ),
    )
    assert report.status is HeavyTailStatus.QUALIFIED
    assert report.bootstrap_lower_bound > 0
    assert report.positive_block_ratio == 1.0

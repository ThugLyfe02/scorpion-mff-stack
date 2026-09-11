from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.hac_edge import HACEdgePolicy, HACEdgeStatus, evaluate_hac_edge

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


def test_hac_gate_discounts_clustered_regime_runs():
    values = ([0.010] * 10 + [-0.006] * 10) * 3
    trades = tuple(_trade(index, value) for index, value in enumerate(values))
    report = evaluate_hac_edge(
        "clustered",
        trades,
        policy=HACEdgePolicy(
            minimum_samples=30,
            max_lag=5,
            minimum_effective_samples=20.0,
        ),
    )
    assert report.mean_return > 0
    assert report.lag1_autocorrelation > 0.70
    assert report.effective_samples < 20
    assert report.status is HACEdgeStatus.FAILED


def test_hac_gate_passes_consistent_low_autocorrelation_edge():
    pattern = [0.004, 0.002, 0.005, 0.001, 0.003]
    values = pattern * 12
    trades = tuple(_trade(index, value) for index, value in enumerate(values))
    report = evaluate_hac_edge(
        "stable",
        trades,
        policy=HACEdgePolicy(
            minimum_samples=30,
            max_lag=5,
            minimum_effective_samples=15.0,
        ),
    )
    assert report.status is HACEdgeStatus.QUALIFIED
    assert report.hac_lower_bound > 0
    assert report.effective_samples >= 15

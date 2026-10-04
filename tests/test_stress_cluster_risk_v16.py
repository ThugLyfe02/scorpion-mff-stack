from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.stress_cluster_risk import (
    StressClusterPolicy,
    StressClusterStatus,
    evaluate_stress_cluster_risk,
)
from scorpion.tail_dependence import TailDependencePolicy, evaluate_tail_dependence

BASE = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
CHANNEL = "968352649437126676"


def _trade(day_index: int, ticker: str, value: float, premium: str = "100") -> CompletedTrade:
    opened = BASE + timedelta(days=day_index)
    gross = Decimal(premium)
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


def _history() -> tuple[CompletedTrade, ...]:
    rows: list[CompletedTrade] = []
    shared_bad = set(range(0, 40, 5))
    independent_bad = set(range(2, 40, 5))
    for day in range(40):
        cluster_return = -0.30 if day in shared_bad else 0.03
        independent_return = -0.08 if day in independent_bad else 0.03
        rows.extend(
            (
                _trade(day, "AAPL", cluster_return, "120"),
                _trade(day, "NVDA", cluster_return, "120"),
                _trade(day, "MSFT", independent_return, "80"),
            )
        )
    return tuple(rows)


def test_stress_cluster_risk_detects_false_diversification():
    trades = _history()
    tail = evaluate_tail_dependence(
        trades,
        policy=TailDependencePolicy(
            tail_fraction=0.20,
            minimum_overlap_days=30,
            minimum_tail_events=5,
            cluster_dependence_threshold=0.60,
        ),
    )
    report = evaluate_stress_cluster_risk(
        trades,
        tail,
        policy=StressClusterPolicy(
            minimum_days=30,
            tail_fraction=0.10,
            maximum_cluster_premium_share=0.60,
            maximum_cluster_es_share=0.70,
            maximum_research_cluster_risk_fraction=0.06,
        ),
    )
    assert report.status is StressClusterStatus.FAIL
    assert report.maximum_cluster_premium_share > 0.70
    assert report.maximum_cluster_es_share > 0.90
    assert report.max_research_cluster_risk_fraction < 0.06
    assert any("latent_cluster" in item for item in report.failures)


def test_stress_cluster_risk_passes_balanced_independent_clusters():
    trades: list[CompletedTrade] = []
    for day in range(40):
        aapl = -0.12 if day % 6 == 0 else 0.03
        nvda = -0.12 if day % 6 == 2 else 0.03
        msft = -0.12 if day % 6 == 4 else 0.03
        trades.extend(
            (
                _trade(day, "AAPL", aapl),
                _trade(day, "NVDA", nvda),
                _trade(day, "MSFT", msft),
            )
        )
    tail = evaluate_tail_dependence(
        tuple(trades),
        policy=TailDependencePolicy(
            tail_fraction=0.20,
            minimum_overlap_days=30,
            minimum_tail_events=5,
            cluster_dependence_threshold=0.70,
        ),
    )
    report = evaluate_stress_cluster_risk(
        tuple(trades),
        tail,
        policy=StressClusterPolicy(
            minimum_days=30,
            maximum_cluster_premium_share=0.50,
            maximum_cluster_es_share=0.60,
        ),
    )
    assert report.status is StressClusterStatus.PASS
    assert report.max_research_cluster_risk_fraction == 0.06

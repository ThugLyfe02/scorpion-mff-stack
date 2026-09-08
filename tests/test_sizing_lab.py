from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.sizing_lab import (
    SizingConstraints,
    SizingReadiness,
    build_sizing_envelope,
    score_segment,
)
from scorpion.strategy_selector import SelectionStatus, evaluate_candidate


def _trade(index: int, return_fraction: str) -> CompletedTrade:
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    invested = Decimal("1000")
    return_value = Decimal(return_fraction)
    return CompletedTrade(
        entry_event_id=f"event-{index}",
        contract_key=f"TSLA|CALL|{300 + index}|2026-12-31",
        channel_id="968352649437126676",
        author_id="author",
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=opened,
        closed_ts_utc=opened + timedelta(minutes=10),
        initial_quantity=1,
        add_count=0,
        trim_count=0,
        gross_premium_in=invested,
        gross_proceeds=invested * (Decimal("1") + return_value),
        pnl=invested * return_value,
        return_fraction=return_value,
        holding_seconds=600.0,
    )


def test_positive_deep_sample_can_become_research_ready():
    values = ["0.12", "0.08", "0.06", "-0.02", "0.10"] * 8
    trades = tuple(_trade(index, value) for index, value in enumerate(values))
    constraints = SizingConstraints(
        min_samples=30,
        max_risk_fraction=0.06,
        candidate_step=0.02,
        horizon_trades=20,
        trials=100,
        max_drawdown_limit=0.50,
        max_drawdown_breach_probability=0.50,
    )
    metrics = score_segment("core", trades, constraints=constraints)
    assert metrics.readiness is SizingReadiness.READY_FOR_RESEARCH
    assert metrics.conservative_edge > 0
    assert metrics.win_rate_lower_90 > 0.5

    envelope = build_sizing_envelope("core", trades, constraints=constraints)
    assert envelope.readiness is SizingReadiness.READY_FOR_RESEARCH
    assert envelope.max_research_risk_fraction > 0
    assert envelope.selected_simulation is not None

    candidate = evaluate_candidate(metrics)
    assert candidate.status is SelectionStatus.SELECTED
    assert "not a profit guarantee" in candidate.reason


def test_small_sample_cannot_emit_sizing_readiness():
    trades = tuple(_trade(index, "0.20") for index in range(8))
    metrics = score_segment("thin", trades)
    assert metrics.readiness is SizingReadiness.INSUFFICIENT_SAMPLE
    envelope = build_sizing_envelope("thin", trades)
    assert envelope.max_research_risk_fraction == 0.0
    assert envelope.selected_simulation is None

"""Paired latency evidence must never mistake missing trades for lost returns."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.latency_value import pair_latency_outcomes


def trade(key: str, outcome: str, day: int = 0) -> CompletedTrade:
    start = datetime(2026, 11, 2, 15, tzinfo=UTC) + timedelta(days=day)
    return CompletedTrade(
        key, "FIXTURE", "channel", "author", next(iter(StrategyBucket)),
        start, start + timedelta(minutes=1), 1, 0, 0,
        Decimal(100), Decimal(100) * (1 + Decimal(outcome)),
        Decimal(100) * Decimal(outcome), Decimal(outcome), 60, True,
    )


def test_selection_bias_is_visible_not_zero_imputed() -> None:
    a, b, c = trade("a", ".1"), trade("b", ".9"), trade("c", "-.8")
    report = pair_latency_outcomes([a, b], [replace(a, return_fraction=Decimal(".05")), c])
    assert report.matched_trades == report.faster_only == report.slower_only == 1
    assert report.paired_mean_return_loss == Decimal(".05")
    assert report.status == "INSUFFICIENT"


def test_unobserved_pair_returns_no_estimate() -> None:
    report = pair_latency_outcomes([trade("a", ".1")], [trade("b", ".9")])
    assert report.matched_trades == 0
    assert report.paired_mean_return_loss is None
    assert report.paired_gross_pnl_loss is None


def test_session_equal_weight_and_research_authority() -> None:
    fast = [trade("a", ".3"), trade("b", ".3"), trade("c", "-.2", day=1)]
    slow = [replace(t, return_fraction=Decimal(0), pnl=Decimal(0)) for t in fast]
    report = pair_latency_outcomes(fast, slow, minimum_pairs=3, minimum_sessions=2)
    assert report.session_equal_mean_return_loss == Decimal(".05")
    assert report.paired_mean_return_loss == Decimal(".4") / 3
    assert report.paired_gross_pnl_loss == Decimal(40)
    assert report.authority == "RESEARCH_ONLY"
    assert report.status == "DESCRIPTIVE_COVERAGE_MET"


def test_duplicate_and_conflicting_identity_refuse() -> None:
    a = trade("a", ".1")
    with pytest.raises(ValueError, match="duplicate"):
        pair_latency_outcomes([a, a], [a])
    with pytest.raises(ValueError, match="identity conflicts"):
        pair_latency_outcomes([a], [replace(a, contract_key="OTHER")])
    assert pair_latency_outcomes([replace(a, depth_evidence_complete=False)], [a]).matched_trades == 0

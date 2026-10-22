"""The pre-trade risk budget: worst-case loss before the order, not after it."""

from __future__ import annotations

import json
import time

import pytest

from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.risk import (
    Admission,
    RiskBudget,
    RiskBudgetError,
    premium_at_risk,
)
from scorpion_mff.types import AlertKind, Position, Quote, Side, Signal

ET = "etf-options"
HIGH = "high-confidence-options"


def pos(ticker: str, contracts: int, price: float, *, channel: str = ET) -> Position:
    return Position(contract_key=f"{ticker}|CALL|100|0DTE", ticker=ticker, side=Side.CALL,
                    contracts=contracts, avg_price=price, opened_ts=0.0, channel=channel)


def test_no_caps_is_a_pass_through():
    budget = RiskBudget()
    assert budget.admit([], ticker="QQQ", qty=14, limit=1.05) == Admission(14, ())


def test_total_premium_cap_shrinks_but_never_grows():
    budget = RiskBudget(max_total_premium_usd=1500.0)
    book = [pos("QQQ", 10, 1.00)]  # $1,000 already at risk
    admission = budget.admit(book, ticker="SPY", qty=20, limit=1.00)
    assert admission.qty == 5
    assert "total_premium" in admission.reasons
    assert admission.qty <= 20


def test_daily_limit_prices_the_loss_that_has_not_happened_yet():
    budget = RiskBudget(daily_loss_limit_usd=1000.0)
    # Already -$500 realised, no open risk, candidate costs $100/contract.
    admission = budget.admit([], ticker="QQQ", qty=10, limit=1.00,
                             realized_today_usd=-500.0)
    assert admission.qty == 5
    assert "daily_worst_case" in admission.reasons


def test_daily_limit_refuses_when_open_premium_already_breaches_it():
    budget = RiskBudget(daily_loss_limit_usd=1000.0)
    book = [pos("QQQ", 20, 1.00)]  # $2,000 at risk, limit is $1,000
    admission = budget.admit(book, ticker="SPY", qty=5, limit=1.00)
    assert admission.qty == 0
    assert admission.accepted is False
    assert "daily_worst_case" in admission.reasons


def test_daily_limit_uses_realised_profit_as_headroom():
    budget = RiskBudget(daily_loss_limit_usd=1000.0)
    admission = budget.admit([], ticker="QQQ", qty=30, limit=1.00,
                             realized_today_usd=500.0)
    assert admission.qty == 15  # $1,500 headroom = 15 contracts


def test_underlying_cap_counts_two_strikes_as_one_bet():
    budget = RiskBudget(max_underlying_premium_usd=1500.0)
    book = [pos("QQQ", 10, 1.00)]  # $1,000 on QQQ
    admission = budget.admit(book, ticker="qqq", qty=10, limit=2.00)
    assert admission.qty == 2  # $500 headroom / $200 per contract
    assert "underlying_premium" in admission.reasons


def test_cluster_cap_treats_index_etfs_as_one_directional_bet():
    budget = RiskBudget(max_cluster_premium_usd=1500.0)
    book = [pos("QQQ", 10, 1.00)]  # $1,000 in the index cluster
    admission = budget.admit(book, ticker="SPY", qty=10, limit=1.00)
    assert admission.qty == 5
    assert "cluster_premium" in admission.reasons


def test_an_unmapped_symbol_is_its_own_cluster():
    budget = RiskBudget(max_cluster_premium_usd=1500.0)
    book = [pos("QQQ", 10, 1.00)]
    # XYZ is not in a declared cluster; QQQ exposure must not cap it.
    admission = budget.admit(book, ticker="XYZ", qty=10, limit=1.00)
    assert admission.qty == 10
    assert admission.reasons == ()


def test_every_cap_is_reported_and_the_minimum_wins():
    budget = RiskBudget(max_total_premium_usd=900.0,
                        max_underlying_premium_usd=800.0,
                        max_cluster_premium_usd=600.0,
                        daily_loss_limit_usd=500.0)
    admission = budget.admit([], ticker="QQQ", qty=10, limit=1.00,
                             realized_today_usd=0.0)
    assert admission.qty == 5
    assert set(admission.reasons) == {"total_premium", "underlying_premium",
                                      "cluster_premium", "daily_worst_case"}


def test_premium_at_risk_is_cost_basis_and_closed_positions_are_free():
    assert premium_at_risk(pos("QQQ", 3, 1.25)) == pytest.approx(375.0)
    assert premium_at_risk(pos("QQQ", 0, 1.25)) == 0.0


def test_budget_rejects_an_unknown_key_instead_of_ignoring_it():
    with pytest.raises(RiskBudgetError, match="unknown key"):
        RiskBudget.from_dict({"version": 1, "max_total_premium": 1000})


def test_budget_rejects_negative_and_non_finite_limits():
    with pytest.raises(RiskBudgetError, match="positive"):
        RiskBudget(max_total_premium_usd=-1)
    with pytest.raises(RiskBudgetError, match="positive"):
        RiskBudget(daily_loss_limit_usd=0)


def test_budget_rejects_a_symbol_in_two_clusters():
    with pytest.raises(RiskBudgetError, match="one symbol, one cluster"):
        RiskBudget(clusters=(frozenset({"QQQ"}), frozenset({"QQQ", "SPY"})))


def test_budget_round_trips_through_json(tmp_path):
    path = tmp_path / "risk-budget.json"
    original = RiskBudget(max_total_premium_usd=3000.0, daily_loss_limit_usd=1500.0,
                          clusters=(frozenset({"QQQ", "SPY"}),))
    original.save(path)
    loaded = RiskBudget.load(path)
    assert loaded == original
    assert json.loads(path.read_text())["version"] == 1


def test_report_shows_worst_case_day_and_headroom():
    budget = RiskBudget(max_total_premium_usd=2000.0, daily_loss_limit_usd=1000.0)
    report = budget.report([pos("QQQ", 5, 1.0)], realized_today_usd=-100.0)
    assert report["premium_at_risk_usd"] == 500.0
    assert report["worst_case_day_usd"] == -600.0
    assert report["daily_headroom_usd"] == 400.0
    assert report["total_headroom_usd"] == 1500.0


# ── engine integration ────────────────────────────────────────────────────────
def _ts(text: str) -> float:
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M"))


def _entry(ts: float, *, channel: str = ET, ticker: str = "QQQ", bid: float = 1.0) -> Signal:
    return Signal(kind=AlertKind.ENTRY, channel=channel, ticker=ticker, side=Side.CALL,
                  strike=719.0, expiry="0DTE", his_bid=bid, raw="entry")


def test_engine_shrinks_the_second_entry_to_the_total_budget():
    ts = _ts("2026-03-10 10:00")
    budget = RiskBudget(max_total_premium_usd=2000.0, daily_loss_limit_usd=2000.0)
    eng = Engine(config=EngineConfig(), fills=FillLog(), risk_budget=budget)

    first = eng.on_signal(_entry(ts, bid=1.05), Quote(1.05, 1.05), now=ts)
    assert len(first) == 1 and first[0].qty == 1  # pipe test: $105 at risk

    second = eng.on_signal(_entry(ts + 60, channel=HIGH, ticker="SPY"),
                           Quote(1.0, 1.0), now=ts + 60)
    assert len(second) == 1
    assert second[0].qty == 18  # ($2,000 - $105) / $100
    assert "budget=total_premium" in second[0].reason


def test_engine_refuses_an_entry_that_would_breach_the_daily_worst_case():
    ts = _ts("2026-03-10 10:00")
    budget = RiskBudget(max_total_premium_usd=10_000.0, daily_loss_limit_usd=100.0)
    eng = Engine(config=EngineConfig(pipe_test=False), fills=FillLog(), risk_budget=budget)
    # $1.00 limit => $100 per contract. The $100 daily budget affords exactly one,
    # not the 15 the locked clip asked for.
    intents = eng.on_signal(_entry(ts, bid=1.00), Quote(1.0, 1.0), now=ts)
    assert len(intents) == 1
    assert intents[0].qty == 1
    assert "budget=daily_worst_case" in intents[0].reason

    # The one contract already at risk exhausts the worst-case budget: a second
    # entry is refused rather than allowed to double the day's possible loss.
    intents = eng.on_signal(_entry(ts + 60, ticker="SPY", bid=0.50),
                            Quote(0.5, 0.5), now=ts + 60)
    assert intents == []
    assert any("risk_budget:daily_worst_case" in f.reason for f in eng.fills.records)


def test_engine_prices_an_add_against_the_existing_position():
    ts = _ts("2026-03-10 10:00")
    budget = RiskBudget(max_total_premium_usd=2000.0)
    eng = Engine(config=EngineConfig(pipe_test=False), fills=FillLog(), risk_budget=budget)
    eng.on_signal(_entry(ts, bid=1.0), Quote(1.0, 1.0), now=ts)  # 15 contracts, $1,500

    add = Signal(kind=AlertKind.ADD, channel=ET, ticker="QQQ", side=Side.CALL,
                 strike=719.0, expiry="0DTE", his_bid=1.0, price=1.0, raw="adding")
    intents = eng.on_signal(add, Quote(1.0, 1.0), now=ts + 60)
    assert len(intents) == 1
    assert intents[0].qty == 5  # $500 headroom / $100
    assert "budget=total_premium" in intents[0].reason

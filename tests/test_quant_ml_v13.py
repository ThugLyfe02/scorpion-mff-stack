from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.domain import EventKind
from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.hierarchical_shrinkage import (
    HierarchicalPolicy,
    evaluate_hierarchical_shrinkage,
)
from scorpion.oof_stacking import (
    StackingExample,
    StackingPolicy,
    StackingStatus,
    evaluate_cross_fitted_stacking,
)
from scorpion.portfolio_cluster import (
    PortfolioClusterPolicy,
    PortfolioClusterStatus,
    evaluate_portfolio_clusters,
)
from scorpion.selection_adjusted import (
    SelectionAdjustedPolicy,
    SelectionAdjustedStatus,
    evaluate_selection_adjusted_performance,
)

BASE = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
CHANNEL = "968352649437126676"
AUTHOR = "author"


def _trade(
    index: int,
    value: str,
    *,
    ticker: str = "AAPL",
    day_offset: int | None = None,
    premium: str = "100",
) -> CompletedTrade:
    day = index if day_offset is None else day_offset
    opened = BASE + timedelta(days=day)
    gross_in = Decimal(premium)
    return_fraction = Decimal(value)
    pnl = gross_in * return_fraction
    return CompletedTrade(
        entry_event_id=f"event-{ticker}-{index}",
        contract_key=f"{ticker}|CALL|200|2026-12-18",
        channel_id=CHANNEL,
        author_id=AUTHOR,
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=opened,
        closed_ts_utc=opened + timedelta(minutes=5),
        initial_quantity=1,
        add_count=0,
        trim_count=0,
        gross_premium_in=gross_in,
        gross_proceeds=gross_in + pnl,
        pnl=pnl,
        return_fraction=return_fraction,
        holding_seconds=300.0,
        depth_evidence_complete=True,
    )


def _fast_portfolio_policy() -> PortfolioClusterPolicy:
    return PortfolioClusterPolicy(
        minimum_days=20,
        maximum_same_ticker_premium_share=0.75,
        maximum_single_day_premium_share=0.10,
        maximum_daily_drawdown=0.50,
        maximum_loss_day_rate=0.70,
        bootstrap_trials=100,
        bootstrap_block_days=3,
        bootstrap_horizon_days=20,
        maximum_research_risk_fraction=0.01,
        risk_step=0.01,
    )


def test_portfolio_cluster_passes_diversified_days_and_rejects_same_ticker_concentration():
    diversified = tuple(
        _trade(
            index,
            "0.05" if index % 4 else "-0.02",
            ticker="AAPL" if index % 2 == 0 else "NVDA",
        )
        for index in range(25)
    )
    passed = evaluate_portfolio_clusters(diversified, policy=_fast_portfolio_policy())
    assert passed.status is PortfolioClusterStatus.PASS
    assert passed.max_research_risk_fraction == 0.01

    concentrated = tuple(
        _trade(index, "0.05", ticker="AAPL")
        for index in range(25)
    )
    failed = evaluate_portfolio_clusters(concentrated, policy=_fast_portfolio_policy())
    assert failed.status is PortfolioClusterStatus.FAIL
    assert any("same_ticker_premium_concentration" in item for item in failed.failures)


def test_empirical_bayes_partial_pooling_shrinks_thin_extreme_group():
    groups = {
        "mature-a": tuple(
            _trade(index, "0.05" if index % 2 == 0 else "0.03", ticker="AAPL")
            for index in range(20)
        ),
        "mature-b": tuple(
            _trade(index + 100, "0.04" if index % 2 == 0 else "0.02", ticker="NVDA")
            for index in range(20)
        ),
        "thin-extreme": tuple(
            _trade(index + 200, "0.30" if index % 2 == 0 else "0.20", ticker="MSFT")
            for index in range(5)
        ),
    }
    report = evaluate_hierarchical_shrinkage(
        groups,
        policy=HierarchicalPolicy(
            minimum_groups=2,
            minimum_total_samples=30,
            minimum_group_samples=5,
        ),
    )
    thin = next(item for item in report.estimates if item.group == "thin-extreme")
    assert thin.posterior_weight_on_sample < 1.0
    assert report.family_mean < thin.posterior_mean < thin.sample_mean


def test_selection_adjusted_performance_requires_edge_above_search_hurdle():
    trades = tuple(
        _trade(index, "0.08" if index % 3 else "0.03", ticker="AAPL")
        for index in range(60)
    )
    report = evaluate_selection_adjusted_performance(
        "ticker:AAPL",
        trades,
        trials_considered=50,
        policy=SelectionAdjustedPolicy(minimum_samples=30, minimum_probability=0.95),
    )
    assert report.status is SelectionAdjustedStatus.QUALIFIED
    assert report.observed_sharpe > report.selection_hurdle_sharpe
    assert report.probability_above_selection_hurdle >= 0.95


def _probability_row(truth: EventKind, *, good: bool) -> dict[EventKind, float]:
    if truth is EventKind.ENTRY:
        return (
            {EventKind.ENTRY: 0.95, EventKind.IGNORE: 0.05}
            if good
            else {EventKind.ENTRY: 0.05, EventKind.IGNORE: 0.95}
        )
    return (
        {EventKind.ENTRY: 0.02, EventKind.IGNORE: 0.98}
        if good
        else {EventKind.ENTRY: 0.95, EventKind.IGNORE: 0.05}
    )


def test_chronological_oof_stacking_downweights_confidently_wrong_model():
    examples: list[StackingExample] = []
    for fold in range(4):
        for index in range(50):
            truth = EventKind.ENTRY if index % 5 == 0 else EventKind.IGNORE
            examples.append(
                StackingExample(
                    event_id=f"{fold}-{index}",
                    fold=fold,
                    truth=truth,
                    model_probabilities={
                        "good": _probability_row(truth, good=True),
                        "bad": _probability_row(truth, good=False),
                    },
                )
            )
    report = evaluate_cross_fitted_stacking(
        examples,
        labels=(EventKind.ENTRY, EventKind.IGNORE),
        policy=StackingPolicy(
            minimum_folds=3,
            minimum_training_samples=50,
            minimum_oos_samples=100,
            iterations=150,
            learning_rate=0.05,
            wrong_action_penalty=6.0,
            maximum_false_action_rate=0.005,
            maximum_wrong_action_rate=0.01,
        ),
    )
    assert report.status is StackingStatus.QUALIFIED
    weights = dict(report.final_weights)
    assert weights["good"] > weights["bad"]
    assert report.false_action_rate == 0.0
    assert report.wrong_action_rate == 0.0
    assert report.oos_accuracy > 0.95

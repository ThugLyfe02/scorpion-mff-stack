from datetime import date, timedelta

from scorpion.conditional_policy_safety import (
    ConditionalPolicyOutcome,
    ConditionalPolicySafetyPolicy,
    ConditionalSafetyStatus,
    evaluate_conditional_policy_safety,
)
from scorpion.cost_adjusted_policy_value import (
    CostAdjustedPolicyOutcome,
    CostAdjustedPolicyValuePolicy,
    CostAdjustedValueStatus,
    evaluate_cost_adjusted_policy_value,
)


def _conditional_rows(*, stressed: bool) -> tuple[ConditionalPolicyOutcome, ...]:
    start = date(2026, 1, 2)
    rows: list[ConditionalPolicyOutcome] = []
    for day_index in range(30):
        market_date = start + timedelta(days=day_index)
        for event_index in range(4):
            slice_key = "stress" if event_index == 3 else "normal"
            improvement = (-0.02 if stressed else 0.01) if slice_key == "stress" else 0.03
            rows.append(
                ConditionalPolicyOutcome(
                    event_id=f"{day_index}-{event_index}",
                    market_date=market_date,
                    slice_key=slice_key,
                    incumbent_reward=0.0,
                    challenger_reward=improvement,
                )
            )
    return tuple(rows)


def _conditional_policy() -> ConditionalPolicySafetyPolicy:
    return ConditionalPolicySafetyPolicy(
        minimum_events=100,
        minimum_days=20,
        minimum_slice_events=20,
        minimum_slice_days=5,
        minimum_slice_coverage=1.0,
        minimum_tail_mean_delta=-0.02,
        minimum_slice_mean_delta=-0.005,
        minimum_slice_lower_bound=-0.01,
        bootstrap_trials=300,
    )


def test_conditional_policy_safety_rejects_hidden_regime_harm():
    report = evaluate_conditional_policy_safety(
        _conditional_rows(stressed=True),
        policy=_conditional_policy(),
    )
    assert report.global_mean_daily_delta > 0
    assert report.status is ConditionalSafetyStatus.FAILED
    stress = next(item for item in report.slice_reports if item.slice_key == "stress")
    assert stress.qualified is False
    assert any("slice_mean_delta" in item for item in stress.failures)


def test_conditional_policy_safety_qualifies_broad_improvement():
    report = evaluate_conditional_policy_safety(
        _conditional_rows(stressed=False),
        policy=_conditional_policy(),
    )
    assert report.status is ConditionalSafetyStatus.QUALIFIED
    assert all(item.qualified for item in report.slice_reports)


def _cost_rows(
    *,
    challenger_gross: float,
    challenger_cost: float,
    challenger_turnover: float = 1.1,
) -> tuple[CostAdjustedPolicyOutcome, ...]:
    start = date(2026, 2, 2)
    rows: list[CostAdjustedPolicyOutcome] = []
    for day_index in range(25):
        for event_index in range(4):
            rows.append(
                CostAdjustedPolicyOutcome(
                    event_id=f"{day_index}-{event_index}",
                    market_date=start + timedelta(days=day_index),
                    fold=day_index % 5,
                    incumbent_gross_reward=0.0,
                    challenger_gross_reward=challenger_gross,
                    incumbent_execution_cost=0.005,
                    challenger_execution_cost=challenger_cost,
                    incumbent_turnover=1.0,
                    challenger_turnover=challenger_turnover,
                )
            )
    return tuple(rows)


def _cost_policy() -> CostAdjustedPolicyValuePolicy:
    return CostAdjustedPolicyValuePolicy(
        minimum_events=100,
        minimum_days=20,
        minimum_folds=4,
        bootstrap_trials=300,
    )


def test_cost_adjusted_policy_value_rejects_gross_edge_erased_by_costs():
    report = evaluate_cost_adjusted_policy_value(
        _cost_rows(challenger_gross=0.02, challenger_cost=0.03),
        policy=_cost_policy(),
    )
    assert report.mean_gross_delta > 0
    assert report.mean_net_delta < 0
    assert report.status is CostAdjustedValueStatus.FAILED
    assert any("mean_net_delta" in item for item in report.failures)


def test_cost_adjusted_policy_value_requires_fold_stable_net_edge():
    report = evaluate_cost_adjusted_policy_value(
        _cost_rows(challenger_gross=0.03, challenger_cost=0.005),
        policy=_cost_policy(),
    )
    assert report.status is CostAdjustedValueStatus.QUALIFIED
    assert report.bootstrap_lower_net_delta > 0
    assert report.positive_fold_ratio == 1.0


def test_cost_adjusted_policy_value_rejects_turnover_explosion():
    report = evaluate_cost_adjusted_policy_value(
        _cost_rows(
            challenger_gross=0.03,
            challenger_cost=0.005,
            challenger_turnover=2.0,
        ),
        policy=_cost_policy(),
    )
    assert report.status is CostAdjustedValueStatus.FAILED
    assert report.turnover_multiple == 2.0
    assert any("turnover_multiple" in item for item in report.failures)

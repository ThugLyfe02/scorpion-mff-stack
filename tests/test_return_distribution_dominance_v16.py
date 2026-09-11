from scorpion.return_distribution_dominance import (
    DistributionDominancePolicy,
    DistributionDominanceStatus,
    PairedReturn,
    evaluate_return_distribution_dominance,
)


def test_higher_mean_with_worse_left_tail_is_rejected():
    rows: list[PairedReturn] = []
    for index in range(120):
        incumbent = 0.02 if index % 5 else -0.05
        challenger = 0.06 if index % 10 else -0.40
        rows.append(PairedReturn(str(index), incumbent, challenger))
    report = evaluate_return_distribution_dominance(
        rows,
        policy=DistributionDominancePolicy(
            minimum_samples=100,
            maximum_lower_partial_shortfall=0.0,
            maximum_q10_degradation=0.02,
        ),
    )
    assert report.challenger_mean > report.incumbent_mean
    assert report.status is DistributionDominanceStatus.FAILED
    assert report.worst_lower_partial_delta < 0
    assert report.second_order_noninferior is False


def test_broad_distribution_improvement_qualifies():
    rows = tuple(
        PairedReturn(
            str(index),
            incumbent_return=(-0.08 if index % 6 == 0 else 0.02),
            challenger_return=(-0.04 if index % 6 == 0 else 0.035),
        )
        for index in range(120)
    )
    report = evaluate_return_distribution_dominance(
        rows,
        policy=DistributionDominancePolicy(minimum_samples=100),
    )
    assert report.status is DistributionDominanceStatus.QUALIFIED
    assert report.qualified is True
    assert report.q10_delta >= 0
    assert report.second_order_noninferior is True

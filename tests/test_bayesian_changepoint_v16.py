from scorpion.bayesian_changepoint import (
    BayesianChangePointPolicy,
    ChangePointStatus,
    evaluate_bayesian_changepoint,
)

POLICY = BayesianChangePointPolicy(
    window_size=120,
    minimum_segment=15,
    changepoint_prior_probability=0.10,
    watch_posterior_probability=0.40,
    changepoint_posterior_probability=0.80,
    minimum_rate_change=0.20,
)


def test_stationary_failure_stream_does_not_fabricate_changepoint():
    rows = tuple(index % 20 == 0 for index in range(100))
    report = evaluate_bayesian_changepoint(rows, policy=POLICY)
    assert report.status in {ChangePointStatus.STABLE, ChangePointStatus.WATCH}
    assert report.degradation is False
    assert abs(report.rate_change) < 0.20


def test_abrupt_failure_jump_is_degradation_changepoint():
    rows = (False,) * 60 + (True,) * 40
    report = evaluate_bayesian_changepoint(rows, policy=POLICY)
    assert report.status is ChangePointStatus.DEGRADATION_CHANGEPOINT
    assert report.degradation is True
    assert report.posterior_changepoint_probability >= 0.80
    assert report.best_split_index is not None
    assert 50 <= report.best_split_index <= 70
    assert report.post_failure_rate > report.pre_failure_rate


def test_abrupt_recovery_is_not_mislabeled_as_degradation():
    rows = (True,) * 50 + (False,) * 50
    report = evaluate_bayesian_changepoint(rows, policy=POLICY)
    assert report.status is ChangePointStatus.IMPROVEMENT_CHANGEPOINT
    assert report.degradation is False
    assert report.post_failure_rate < report.pre_failure_rate

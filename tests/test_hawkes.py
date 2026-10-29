"""Alert clustering: the estimator recovers an ordering, and the queue arithmetic is exposed."""

import math

import pytest

from scorpion.hawkes import (
    MIN_EVENTS,
    HawkesError,
    Params,
    bootstrap_branching,
    fano_branching,
    fit,
    intensity_terms,
    interval,
    log_likelihood,
    queue_pressure,
    render,
    report,
    simulate,
)

BETA = 1 / 300.0


def synthetic(branching, horizon=400000, seed=3):
    params = Params(mu=0.0008, alpha=branching * BETA, beta=BETA)
    return params, simulate(params, horizon=horizon, seed=seed)


def test_intensity_terms_start_empty_and_decay():
    times = [0.0, 10.0, 20.0]
    rec = intensity_terms(times, Params(1.0, 1.0, 0.1))
    assert rec[0] == 0.0
    assert rec[1] == pytest.approx(math.exp(-0.1 * 10.0))
    assert rec[2] > rec[1]


def test_bad_parameters_are_refused():
    with pytest.raises(HawkesError):
        intensity_terms([1.0], Params(0.0, 1.0, 1.0))


def test_the_log_likelihood_is_finite_and_negative():
    _params, events = synthetic(0.5)
    fitted = fit(events, horizon=400000)
    value = log_likelihood(events, fitted, horizon=400000)
    assert math.isfinite(value) and value < 0


def test_more_events_come_from_more_branching():
    _p1, few = synthetic(0.2)
    _p2, many = synthetic(0.7)
    assert len(many) > len(few)


def test_the_branching_estimate_is_ordered_and_close():
    estimates = []
    for target in (0.2, 0.5, 0.7):
        _params, events = synthetic(target)
        estimates.append(fit(events, horizon=400000).branching)
    assert estimates[0] < estimates[1] < estimates[2]
    assert estimates[2] == pytest.approx(0.7, abs=0.3)


def test_a_thin_sample_is_refused():
    with pytest.raises(HawkesError):
        fit([float(i) for i in range(MIN_EVENTS - 5)], horizon=1000.0)


def test_a_horizon_before_the_last_event_is_refused():
    _params, events = synthetic(0.5)
    with pytest.raises(HawkesError):
        fit(events, horizon=events[-1])


def test_a_poisson_like_series_has_no_branching():
    times = [float(i) for i in range(200)]
    assert fano_branching(times, horizon=200.0, bins=20) == 0.0


def test_the_queue_isolates_the_effect_of_clustering():
    _params, events = synthetic(0.7)
    payload = queue_pressure(events, capacity=2, hold_seconds=1800.0)
    assert 0 <= payload["blocked_observed"] <= payload["events"]
    assert payload["excess_from_clustering"] >= 0


def test_bad_queue_inputs_are_refused():
    with pytest.raises(HawkesError):
        queue_pressure([float(i) for i in range(50)], capacity=0, hold_seconds=10)


def test_the_bootstrap_interval_brackets_zero_or_above():
    _params, events = synthetic(0.5)
    draws = bootstrap_branching(events, horizon=400000, draws=8, seed=1)
    lo, hi = interval(draws)
    assert lo <= hi


def test_the_report_and_render_carry_the_queue_arithmetic():
    _params, events = synthetic(0.6)
    payload = report(events, horizon=400000, draws=8, seed=1)
    assert payload["caveats"]
    assert "branching ratio" in render(payload)
    assert payload["queue"]["blocked_rate"] >= 0

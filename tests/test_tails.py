"""Tail estimation: recover a known Pareto tail, and report what the sample cannot say."""

import random

import pytest

from scorpion.tails import (
    Fit,
    TailError,
    bootstrap_shape,
    choose_threshold,
    fit,
    interval,
    losses_from_r,
    mean_residual_life,
    render,
    report,
    tail_beyond,
)


def gpd_losses(n=400, shape=0.3, scale=1.0, threshold=0.5, seed=8):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        u = rng.random()
        excess = (scale / shape) * ((1.0 - u) ** (-shape) - 1.0)
        out.append(threshold + excess)
    return out


def test_losses_are_the_negative_of_returns():
    assert losses_from_r([1.0, -0.5, 0.0]) == [-1.0, 0.0, 0.5]


def test_a_thin_sample_is_refused():
    with pytest.raises(TailError):
        fit([1.0, 2.0, 3.0])


def test_the_fit_recovers_a_known_shape():
    fitted = fit(gpd_losses(), threshold=0.5)
    assert fitted.shape == pytest.approx(0.3, abs=0.15)
    assert fitted.scale > 0
    assert fitted.exceedances == 400


def test_a_finite_mean_requires_shape_below_one():
    assert Fit(0.3, 1.0, 0.5, 30, 100).finite_mean
    assert not Fit(1.4, 1.0, 0.5, 30, 100).finite_mean


def test_the_quantile_rises_as_the_probability_falls():
    fitted = fit(gpd_losses(), threshold=0.5)
    assert fitted.quantile(0.10) < fitted.quantile(0.01) < fitted.quantile(0.001)


def test_bad_probabilities_and_thresholds_are_refused():
    fitted = Fit(0.3, 1.0, 0.5, 30, 100)
    with pytest.raises(TailError):
        fitted.quantile(1.5)
    with pytest.raises(TailError):
        tail_beyond(fitted, 0.2)
    with pytest.raises(TailError):
        fit(gpd_losses(), threshold=99.0)


def test_mean_residual_life_counts_exceedances():
    rows = mean_residual_life(gpd_losses(), [0.5, 1.0, 2.0])
    assert rows[0]["exceedances"] > rows[-1]["exceedances"]


def test_a_threshold_is_chosen_only_when_enough_exceedances_remain():
    losses = gpd_losses()
    cut = choose_threshold(losses)
    assert sum(1 for v in losses if v > cut) >= 20


def test_the_bootstrap_shape_interval_brackets_a_positive_shape():
    losses = gpd_losses()
    shapes = bootstrap_shape(losses, threshold=0.5, draws=80, seed=3)
    lo, hi = interval(shapes)
    assert lo < hi
    assert hi > 0.0


def test_a_bounded_tail_reports_zero_probability_beyond_its_support():
    # negative shape implies a finite upper end point
    bounded = Fit(-0.5, 1.0, 0.5, 40, 100)
    outcome = tail_beyond(bounded, 50.0)
    assert outcome["bounded_out"] is True
    assert outcome["probability"] == 0.0


def test_the_probability_of_a_bigger_loss_falls():
    fitted = fit(gpd_losses(), threshold=0.5)
    small = tail_beyond(fitted, 1.0)["probability"]
    large = tail_beyond(fitted, 3.0)["probability"]
    assert 0.0 < large < small


def test_the_report_is_honest_about_the_corpus_boundary():
    returns = [-value / 10.0 for value in gpd_losses(n=200, seed=4)]
    payload = report(returns, draws=60, seed=1)
    assert payload["empirical_max_loss"] > 0
    assert payload["caveats"]
    assert "GPD" in render(payload)

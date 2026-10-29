"""Impact per size: recover a known exponent, and refuse to call a linear fit curved."""

import math

import pytest

from scorpion.slippage import (
    SlippageError,
    fit_impact,
    observations,
    predict,
    render,
    report,
    table,
)


def synthetic(exponent, sizes=(1, 2, 3, 5, 8, 10, 20, 40), cost_at_one=5.0):
    return [
        {"contracts": s, "cost_bps": cost_at_one * (s ** exponent)} for s in sizes
    ]


def test_the_exponent_is_recovered_from_generated_data():
    fit = fit_impact(observations(synthetic(0.5)))
    assert fit.exponent == pytest.approx(0.5, abs=0.02)
    assert fit.concave


def test_a_linear_series_is_not_called_concave():
    fit = fit_impact(observations(synthetic(1.0)))
    assert fit.linear_not_rejected
    assert fit.to_dict()["verdict"] == "linear_adequate"


def test_a_convex_series_is_flagged_rather_than_forced_concave():
    fit = fit_impact(observations(synthetic(1.6)))
    assert not fit.concave
    assert fit.to_dict()["verdict"] == "convex_impact"


def test_too_few_observations_are_refused():
    with pytest.raises(SlippageError):
        fit_impact([(1.0, 5.0), (2.0, 6.0)])


def test_identical_sizes_are_refused():
    rows = [(5.0, 5.0)] * 10
    with pytest.raises(SlippageError):
        fit_impact(rows)


def test_non_positive_fills_are_dropped_not_coerced():
    rows = [{"contracts": 0, "cost_bps": 5.0}, {"contracts": 2, "cost_bps": -1.0}]
    assert observations(rows) == []


def test_zero_costs_are_dropped_rather_than_flattening_the_curve():
    rows = synthetic(0.5) + [{"contracts": 60, "cost_bps": 0.0}]
    assert fit_impact(observations(rows)).exponent == pytest.approx(0.5, abs=0.02)


def test_prediction_scales_by_the_exponent():
    fit = fit_impact(observations(synthetic(0.5)))
    doubled = predict(fit, contracts=2, reference_cost=5.0, reference_size=1)
    assert doubled == pytest.approx(5.0 * math.sqrt(2.0), rel=1e-6)


def test_a_bad_anchor_is_refused():
    fit = fit_impact(observations(synthetic(0.5)))
    with pytest.raises(SlippageError):
        predict(fit, contracts=0, reference_cost=5.0, reference_size=1)
    with pytest.raises(SlippageError):
        predict(fit, contracts=1, reference_cost=0.0, reference_size=1)


def test_the_table_shows_what_a_linear_model_would_have_claimed():
    fit = fit_impact(observations(synthetic(0.5)))
    rows = table(fit, reference_cost=5.0, reference_size=1)
    assert rows[-1]["cost_bps_linear"] > rows[-1]["cost_bps_curve"]
    assert rows[-1]["linear_overstates_by_bps"] > 0


def test_the_report_carries_caveats_and_renders():
    payload = report(synthetic(0.5))
    assert payload["caveats"]
    assert payload["anchor"]["contracts"] == 40
    assert "impact exponent" in render(payload)

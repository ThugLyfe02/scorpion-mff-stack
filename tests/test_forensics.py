"""Refusal forensics: a rung that avoided a loss is a saving, and an unmeasured refusal is unpriced."""

import pytest

from scorpion.forensics import (
    ForensicsError,
    Rung,
    attribute,
    decisive,
    loosening_case,
    render,
)


def rows():
    return [
        {"rung": "same_day_reentry", "counterfactual_r": 1.0},
        {"rung": "same_day_reentry", "counterfactual_r": -0.5},
        {"rung": "same_day_reentry", "counterfactual_r": 0.8},
        {"rung": "excluded_ticker", "counterfactual_r": None},
        {"rung": "excluded_ticker", "counterfactual_r": None},
        {"rung": "dislocation_gate", "counterfactual_r": -2.0},
        {"rung": "dislocation_gate", "counterfactual_r": -1.0},
        {"rung": "dislocation_gate", "counterfactual_r": -0.4},
    ]


def test_missed_gain_and_avoided_loss_are_kept_apart():
    report = attribute(rows(), usd_per_r=1000.0)
    rung = report.rungs["same_day_reentry"]
    assert rung.priced == 3
    assert rung.missed_gain == pytest.approx(1800.0)
    assert rung.avoided_loss == pytest.approx(500.0)
    assert rung.net_cost == pytest.approx(1300.0)


def test_a_rung_that_avoids_losses_has_negative_cost():
    report = attribute(rows(), usd_per_r=1000.0)
    assert report.rungs["dislocation_gate"].net_cost < 0
    assert report.rungs["dislocation_gate"].to_dict()["verdict"] == "earning_its_keep"


def test_unpriced_refusals_are_counted_and_never_imputed():
    report = attribute(rows())
    rung = report.rungs["excluded_ticker"]
    assert rung.refusals == 2 and rung.priced == 0
    assert rung.net_cost == 0.0
    assert rung.to_dict()["verdict"] == "unpriced"
    assert report.unpriced == 2


def test_the_ranking_is_worst_first():
    ranked = attribute(rows(), usd_per_r=1000.0).ranked()
    assert ranked[0]["rung"] == "same_day_reentry"
    assert ranked[-1]["net_cost_usd"] <= ranked[0]["net_cost_usd"]


def test_only_rungs_with_enough_priced_refusals_are_decisive():
    report = attribute(rows())
    names = [row["rung"] for row in decisive(report, minimum=3)]
    assert "same_day_reentry" in names
    assert "excluded_ticker" not in names


def test_the_loosening_case_refuses_to_decide_on_thin_evidence():
    report = attribute(rows())
    case = loosening_case(report, "excluded_ticker")
    assert case["verdict"] == "keep"
    assert "too few" in case["note"]


def test_the_loosening_case_names_the_dollars():
    report = attribute(rows(), usd_per_r=1000.0)
    case = loosening_case(report, "same_day_reentry")
    assert case["verdict"] == "loosen"
    assert "1,300" in case["note"]


def test_an_unknown_rung_is_refused():
    with pytest.raises(ForensicsError):
        loosening_case(attribute(rows()), "no_such_rung")


def test_a_bad_rate_is_refused():
    with pytest.raises(ForensicsError):
        attribute(rows(), usd_per_r=0.0)


def test_net_cost_is_the_difference_of_the_two_columns():
    rung = Rung("x", refusals=2, priced=2, avoided_loss=100.0, missed_gain=650.0)
    assert rung.net_cost == pytest.approx(550.0)


def test_render_names_the_rungs_and_the_total():
    text = render(attribute(rows(), usd_per_r=1000.0))
    assert "net cost" in text and "same_day_reentry" in text

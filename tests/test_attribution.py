"""Attribution: one dimension per variant, exact deltas, and no theatre about significance."""

from decimal import Decimal

import pytest

from scorpion.attribution import (
    BASELINE,
    Variant,
    VariantError,
    attribute,
    render,
    run_variant,
    worst_and_best,
)
from scorpion.ledger import load_csv

D = Decimal


@pytest.fixture(scope="module")
def ledger():
    return load_csv("mff-backtest/trades.csv")


def test_a_variant_may_change_only_one_dimension():
    with pytest.raises(VariantError):
        Variant("both", {"max_open_positions": 3}, exit_haircut=0.1)


def test_an_unknown_policy_field_is_refused(ledger, tmp_path):
    with pytest.raises(VariantError):
        run_variant(ledger, Variant("typo", {"max_open_positons": 3}), db_path=tmp_path / "a.db")


def test_the_baseline_verifies(ledger, tmp_path):
    outcome = run_variant(ledger, BASELINE, db_path=tmp_path / "b.db")
    assert outcome["verified"] is True
    assert outcome["taken"] == 101   # the log now records its policy


def test_a_haircut_cannot_be_applied_twice_or_out_of_range():
    with pytest.raises(VariantError):
        Variant("bad", exit_haircut=1.5)


def test_attribution_measures_each_variant_against_the_same_baseline(ledger, tmp_path):
    payload = attribute(
        ledger,
        (Variant("no_markup", {"price_limit_markup": D("0.00")}, note="exact price"),
         Variant("exit_haircut_10", exit_haircut=0.10, note="worse exits")),
        db_path_for=lambda name: tmp_path / f"{name}.db",
    )
    assert payload["baseline"]["taken"] == 101
    assert len(payload["variants"]) == 2
    assert all(row["verified"] for row in payload["variants"])
    assert payload["method"]["store_per_variant"] is True
    # an exit haircut can only reduce realised R
    haircut = next(r for r in payload["variants"] if r["variant"] == "exit_haircut_10")
    assert haircut["delta_r"] <= 0


def test_the_render_names_every_variant(ledger, tmp_path):
    payload = attribute(
        ledger, (Variant("wider_gate", {"max_dislocation_from_reference": D("0.40")},
                         note="40% gate"),),
        db_path_for=lambda name: tmp_path / f"{name}.db",
    )
    text = render(payload)
    assert "baseline" in text and "wider_gate" in text


def test_worst_and_best_are_ordered_by_delta(ledger, tmp_path):
    payload = attribute(
        ledger,
        (Variant("no_markup", {"price_limit_markup": D("0.00")}),
         Variant("exit_haircut_25", exit_haircut=0.25)),
        db_path_for=lambda name: tmp_path / f"{name}.db",
    )
    extremes = worst_and_best(payload)
    assert extremes["most_expensive"]["delta_r"] <= extremes["most_valuable"]["delta_r"]

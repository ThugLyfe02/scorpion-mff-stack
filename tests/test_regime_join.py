"""Regime join: labels, refusals, and deltas that need evidence on both sides."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scorpion.regime_join import (
    cell_stats,
    contract_key,
    coverage,
    delta_by_regime,
    headline,
    per_regime,
    render,
    report,
    tag,
    tagged_from_rows,
)

D = Decimal
DAY = date(2026, 9, 1)


def trade(r="1.0", channel="etf-options", day=DAY, identity=None):
    """`identity` is what the delta join matches on, so two runs must share it."""
    return SimpleNamespace(ticker="QQQ", side="CALL", strike=D("719"), day=day,
                           identity=identity or f"id-{r}-{day}", channel=channel,
                           r_multiple=None if r is None else D(str(r)))


def ledger(n=6, r="1.0"):
    return SimpleNamespace(trades=[trade(r=r) for _ in range(n)])


def classified(regime="calm_trend", day=DAY):
    key = f"QQQ|CALL|719|{day.isoformat()}"
    return {key: SimpleNamespace(regime=regime)}


def test_the_contract_key_matches_the_loader_convention():
    assert contract_key(trade()) == "QQQ|CALL|719|2026-09-01"


def test_an_unlabelled_trade_becomes_unknown_not_a_default():
    tagged = tag(ledger(2), {})
    assert all(t.regime == "unknown" for t in tagged)


def test_tagging_carries_the_regime_and_the_r():
    tagged = tag(ledger(2), classified("volatile_chop"))
    assert tagged[0].regime == "volatile_chop" and tagged[0].r_multiple == 1.0


def test_a_trade_without_an_r_is_tagged_but_excluded_from_stats():
    tagged = tag(ledger(6, r=None), classified())
    assert all(t.r_multiple is None for t in tagged)
    assert "withheld" in cell_stats(tagged, "calm_trend")


def test_coverage_reports_labelled_and_unlabelled():
    tagged = tag(ledger(6), classified())
    payload = coverage(tagged)
    assert payload["labelled"] == 6 and payload["labelled_pct"] == 100.0


def test_a_thin_cell_is_withheld():
    assert "withheld" in cell_stats(tag(ledger(3), classified()), "calm_trend")


def test_a_populated_cell_reports_statistics():
    tagged = tag(ledger(6), classified())
    payload = cell_stats(tagged, "calm_trend")
    assert payload["n"] == 6 and payload["mean_r"] == 1.0 and payload["win_rate"] == 100.0


def test_per_regime_covers_every_label_present():
    tagged = tag(ledger(6), classified())
    assert set(per_regime(tagged)) == {"calm_trend"}


def test_a_delta_needs_evidence_on_both_sides():
    baseline = tag(ledger(6), classified())
    thin = [trade(identity=f"a{i}") for i in range(2)]
    delta = delta_by_regime(baseline, tag(SimpleNamespace(trades=thin), classified()))
    assert "withheld" in delta["calm_trend"]


def test_a_delta_is_per_trade_not_per_cell():
    ids = [f"same-{i}" for i in range(6)]
    baseline = tag(SimpleNamespace(trades=[trade(r="1.0", identity=i) for i in ids]), classified())
    variant = tag(SimpleNamespace(trades=[trade(r="0.5", identity=i) for i in ids]), classified())
    delta = delta_by_regime(baseline, variant)["calm_trend"]
    assert delta["delta_r_per_trade"] == pytest.approx(-0.5)


def test_the_report_states_its_caveats_and_renders():
    payload = report(ledger(6), classified())
    assert payload["caveats"] and payload["floor"] == 5
    assert "labelled" in render(payload)
    assert headline(payload)["regimes_with_evidence"] == 1


def test_rows_round_trip_to_tagged():
    tagged = tag(ledger(2), classified())
    rebuilt = tagged_from_rows([t.to_dict() for t in tagged])
    assert rebuilt[0].regime == tagged[0].regime
    assert rebuilt[0].day == tagged[0].day


def test_a_missing_row_field_is_refused_rather_than_defaulted():
    with pytest.raises(KeyError):
        tagged_from_rows([{"identity": "x"}])

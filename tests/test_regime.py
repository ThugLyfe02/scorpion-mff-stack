"""Regime: computed features, a labelled cross, and stats that withhold thin cells."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scorpion.regime import (
    MIN_CELL,
    REGIMES,
    RegimeError,
    Thresholds,
    classify,
    classify_sessions,
    conditional_r,
    counts_by_regime,
    describe_thresholds,
    features_from_closes,
    render,
    report,
    summarise,
    transition_matrix,
)

D = Decimal
DAY = date(2026, 9, 1)


def trending(n=20, start=1.0, step=0.01):
    return [start + step * i for i in range(n)]


def choppy(n=20):
    return [1.0 + (0.02 if i % 2 else -0.02) for i in range(n)]


# ── features ───────────────────────────────────────────────────────────────
def test_a_monotone_series_has_positive_slope_and_trend():
    features = features_from_closes(DAY, trending())
    assert features.slope > 0
    assert features.trend_strength > 0
    assert features.total_return > 0


def test_an_oscillating_series_has_little_trend():
    assert abs(features_from_closes(DAY, choppy()).trend_strength) < 1.0


def test_a_flat_series_has_no_volatility():
    features = features_from_closes(DAY, [1.0] * 10)
    assert features.realised_vol == 0.0
    assert features.total_return == 0.0


def test_too_few_closes_are_refused():
    with pytest.raises(RegimeError):
        features_from_closes(DAY, [1.0])


def test_non_positive_prices_are_refused():
    with pytest.raises(RegimeError):
        features_from_closes(DAY, [1.0, 0.0, 1.0])


def test_atr_uses_highs_and_lows_when_given():
    closes = [1.0, 1.0, 1.0, 1.0]
    highs = [1.5, 1.5, 1.5, 1.5]
    lows = [0.5, 0.5, 0.5, 0.5]
    with_atr = features_from_closes(DAY, closes, highs, lows)
    without = features_from_closes(DAY, closes)
    assert with_atr.atr_pct > without.atr_pct


# ── classification ─────────────────────────────────────────────────────────
def test_a_calm_flat_session_is_calm_chop():
    assert classify(features_from_closes(DAY, choppy())).regime == "calm_chop"


def test_a_trending_session_is_a_trend_regime():
    assert classify(features_from_closes(DAY, trending(step=0.05))).regime.endswith("trend")


def test_a_violent_session_is_volatile():
    violent = [1.0, 1.4, 0.8, 1.5, 0.7, 1.6, 0.6, 1.7, 0.5, 1.8]
    assert classify(features_from_closes(DAY, violent)).regime.startswith("volatile_")


def test_a_short_session_is_unknown_not_guessed():
    entry = classify(features_from_closes(DAY, [1.0, 1.1]))
    assert entry.regime == "unknown" and "bars" in entry.reason


def test_every_label_is_one_of_the_four():
    for series in (trending(), choppy(), trending(step=0.1)):
        assert classify(features_from_closes(DAY, series)).regime in REGIMES


def test_thresholds_must_be_positive():
    with pytest.raises(RegimeError):
        Thresholds(vol_split=0)
    with pytest.raises(RegimeError):
        Thresholds(min_bars=1)


def test_classify_sessions_skips_unclassifiable_sets():
    good = SimpleNamespace(day=DAY, bars=tuple(
        SimpleNamespace(close=D(str(c)), high=D(str(c * 1.01)), low=D(str(c * 0.99)))
        for c in trending()
    ))
    bad = SimpleNamespace(day=DAY, bars=(SimpleNamespace(close=D("1"), high=D("1"), low=D("1")),))
    out = classify_sessions({"good": good, "bad": bad})
    assert out["good"].regime != "unknown"
    assert out["bad"].regime == "unknown"


# ── conditional performance ────────────────────────────────────────────────
def _ledger(regime_label, n, r_value="1.0"):
    trades = [
        SimpleNamespace(ticker="QQQ", side="CALL", strike=D("719"), day=DAY,
                        identity=f"t{i}", r_multiple=D(r_value))
        for i in range(n)
    ]
    classified = {f"QQQ|CALL|719|{DAY}": SimpleNamespace(regime=regime_label,
                                                        features=SimpleNamespace(day=DAY))}
    return SimpleNamespace(trades=trades), classified


def test_a_thin_regime_cell_is_withheld():
    ledger, classified = _ledger("calm_trend", 3)
    payload = conditional_r(ledger, classified)
    assert "withheld" in payload["regimes"]["calm_trend"]


def test_a_populated_cell_reports_its_statistics():
    ledger, classified = _ledger("calm_trend", MIN_CELL)
    payload = conditional_r(ledger, classified)
    cell = payload["regimes"]["calm_trend"]
    assert cell["n"] == MIN_CELL and cell["mean_r"] == 1.0 and cell["win_rate"] == 100.0


def test_unclassifiable_trades_are_counted_not_folded_in():
    ledger, classified = _ledger("unknown", 6)
    payload = conditional_r(ledger, classified)
    assert payload["unclassified_trades"] == 6


# ── transitions ────────────────────────────────────────────────────────────
def test_persistence_counts_repeats():
    labels = [(date(2026, 9, 1), "calm_trend"), (date(2026, 9, 2), "calm_trend"),
              (date(2026, 9, 3), "volatile_chop")]
    matrix = transition_matrix(labels)
    assert matrix["persistence_pct"]["calm_trend"] == 50.0


def test_a_single_observation_has_no_transitions():
    assert transition_matrix([(date(2026, 9, 1), "calm_trend")])["transitions"] == {}


# ── report ─────────────────────────────────────────────────────────────────
def test_the_report_states_its_caveats_and_renders():
    ledger, _ = _ledger("calm_trend", 6)
    barsets = {"QQQ|CALL|719|2026-09-01": SimpleNamespace(day=DAY, bars=tuple(
        SimpleNamespace(close=D(str(c)), high=D(str(c * 1.01)), low=D(str(c * 0.99)))
        for c in trending()
    ))}
    payload = report(ledger, barsets)
    assert payload["caveats"] and payload["sessions"] == 1
    assert "sessions classified" in render(payload)
    assert counts_by_regime([SimpleNamespace(regime="calm_trend")]) == {"calm_trend": 1}
    assert "vol>=" in describe_thresholds(Thresholds())
    assert summarise(payload)["cells_withheld"] >= 0

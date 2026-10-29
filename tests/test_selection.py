"""Selection: premium bands with evidence, and a strike inversion that agrees with the delta proxy."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scorpion.exposure import Leg, delta_proxy
from scorpion.selection import (
    DEFAULT_BANDS,
    Band,
    SelectionError,
    band_stats,
    breakeven_premium,
    describe,
    gate,
    nearest_trade_strike,
    premium_pairs,
    strike_band,
    strike_for_delta,
    summary,
    table,
)

D = Decimal
AS_OF = date(2026, 9, 1)


def ledger(pairs):
    return SimpleNamespace(trades=[
        SimpleNamespace(entry_bid=D(str(premium)), r_multiple=D(str(r)))
        for premium, r in pairs
    ])


# ── bands ──────────────────────────────────────────────────────────────────
def test_a_band_is_half_open():
    band = Band(1.0, 2.0)
    assert band.contains(1.0) and band.contains(1.99) and not band.contains(2.0)


def test_a_malformed_band_is_refused():
    with pytest.raises(SelectionError):
        Band(2.0, 1.0)


def test_a_thin_band_is_withheld():
    stats = band_stats([(1.5, 1.0)] * 2, Band(1.0, 2.0))
    assert stats.withheld and not stats.positive


def test_a_populated_band_reports_mean_and_win_rate():
    stats = band_stats([(1.5, 1.0), (1.5, -0.5), (1.5, 2.0)], Band(1.0, 2.0), floor=3)
    assert stats.n == 3 and stats.mean_r == pytest.approx(0.8333, abs=1e-3)
    assert stats.win_rate == pytest.approx(66.67, abs=0.01)


def test_statistics_ignore_premiums_outside_the_band():
    stats = band_stats([(0.2, 5.0), (1.5, 1.0)] * 3, Band(1.0, 2.0), floor=3)
    assert stats.n == 3 and stats.mean_r == 1.0


# ── table and breakeven ────────────────────────────────────────────────────
def test_the_table_withholds_bands_without_evidence():
    payload = table(ledger([(1.5, 1.0)] * 6), floor=5)
    labels = {row["band"]["label"]: row for row in payload["bands"]}
    assert labels["mid"]["withheld"] is False
    assert labels["penny"]["withheld"] is True


def test_breakeven_is_the_top_of_the_last_positive_band():
    rows = [
        band_stats([(0.75, 0.5)] * 6, Band(0.5, 1.0), floor=5),
        band_stats([(1.5, -0.2)] * 6, Band(1.0, 2.0), floor=5),
    ]
    assert breakeven_premium(rows) == 1.0


def test_breakeven_is_none_when_nothing_has_evidence():
    assert breakeven_premium([band_stats([], Band(1.0, 2.0))]) is None


def test_the_gate_refuses_a_band_with_no_evidence():
    decision = gate(ledger([(1.5, 1.0)] * 6), 0.25, bands=DEFAULT_BANDS)
    assert decision["accept"] is False and decision["reason"] == "no_evidence_for_band"


def test_the_gate_refuses_a_negative_expectation():
    decision = gate(ledger([(1.5, -0.5)] * 6), 1.5)
    assert decision["accept"] is False and decision["reason"] == "negative_expectation"


def test_the_gate_accepts_a_positive_expectation():
    decision = gate(ledger([(1.5, 0.9)] * 6), 1.5)
    assert decision["accept"] is True and decision["expected_r"] > 0


def test_premium_pairs_skip_trades_without_an_r():
    mixed = SimpleNamespace(trades=[
        SimpleNamespace(entry_bid=D("1.5"), r_multiple=D("1.0")),
        SimpleNamespace(entry_bid=D("1.5"), r_multiple=None),
    ])
    assert len(premium_pairs(mixed)) == 1


def test_describe_and_summary_agree_on_withheld_counts():
    payload = table(ledger([(1.5, 1.0)] * 6), floor=5)
    assert "mid" in describe(payload)
    assert summary(payload)["bands_withheld"] == len(DEFAULT_BANDS) - 1


# ── strike selection ──────────────────────────────────────────────────────
def test_the_at_the_money_delta_gives_the_spot_strike():
    assert strike_for_delta(720.0, side="CALL", target_delta=0.5, expiry=AS_OF,
                            as_of=AS_OF) == pytest.approx(720.0)


def test_a_higher_target_delta_requires_a_lower_call_strike():
    high = strike_for_delta(720.0, side="CALL", target_delta=0.7, expiry=AS_OF, as_of=AS_OF)
    low = strike_for_delta(720.0, side="CALL", target_delta=0.3, expiry=AS_OF, as_of=AS_OF)
    assert high < 720.0 < low


def test_the_inversion_round_trips_through_the_forward_proxy():
    """The selector and the cap must agree about what delta a strike carries."""
    strike = strike_for_delta(720.0, side="PUT", target_delta=0.4, expiry=AS_OF, as_of=AS_OF)
    leg = Leg("QQQ", "PUT", D(str(round(strike, 2))), AS_OF, 1, D("1.00"), D("720"))
    assert abs(abs(delta_proxy(leg, as_of=AS_OF)) - 0.4) < 0.01


def test_put_strikes_follow_moneyness_not_side():
    """|delta| 0.6 on a put is IN the money, so its strike is above spot; an OTM put is below."""
    in_the_money = strike_for_delta(720.0, side="PUT", target_delta=0.6,
                                    expiry=AS_OF, as_of=AS_OF)
    out_of_the_money = strike_for_delta(720.0, side="PUT", target_delta=0.3,
                                        expiry=AS_OF, as_of=AS_OF)
    assert in_the_money > 720.0 > out_of_the_money


def test_bad_inputs_are_refused():
    with pytest.raises(SelectionError):
        strike_for_delta(0.0, side="CALL", target_delta=0.5, expiry=AS_OF, as_of=AS_OF)
    with pytest.raises(SelectionError):
        strike_for_delta(720.0, side="STRADDLE", target_delta=0.5, expiry=AS_OF, as_of=AS_OF)
    with pytest.raises(SelectionError):
        strike_for_delta(720.0, side="CALL", target_delta=1.5, expiry=AS_OF, as_of=AS_OF)


def test_the_strike_band_orders_by_distance():
    rows = strike_band(720.0, side="CALL", expiry=AS_OF, as_of=AS_OF)
    distances = [row["distance_pct"] for row in rows]
    assert distances == sorted(distances, reverse=True)


def test_the_nearest_listed_strike_is_chosen():
    assert nearest_trade_strike([700, 710, 720, 730], 712.4) == 710
    assert nearest_trade_strike([], 712.4) is None

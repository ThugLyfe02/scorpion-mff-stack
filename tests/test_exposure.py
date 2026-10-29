"""Exposure: correlated book, delta proxy, and caps that refuse for a named reason."""

from datetime import date
from decimal import Decimal

import pytest

from scorpion.exposure import (
    CORRELATION,
    ExposureError,
    Leg,
    Limits,
    assess,
    candidate_fits,
    correlation,
    delta_proxy,
    from_positions,
    leg_exposure,
    max_size_within,
    render,
    summarise,
)

D = Decimal
AS_OF = date(2026, 9, 1)


def leg(ticker="QQQ", side="CALL", strike="720", contracts=1, premium="1.20", spot="720",
        expiry=date(2026, 9, 1)):
    return Leg(ticker, side, D(strike), expiry, contracts, D(premium), D(spot))


# ── the delta proxy ────────────────────────────────────────────────────────
def test_at_the_money_delta_is_near_a_half():
    assert abs(delta_proxy(leg(strike="720", spot="720"), as_of=AS_OF) - 0.5) < 0.02


def test_calls_are_positive_and_puts_negative():
    assert delta_proxy(leg(side="CALL"), as_of=AS_OF) > 0
    assert delta_proxy(leg(side="PUT"), as_of=AS_OF) < 0


def test_deep_in_the_money_call_approaches_one():
    assert delta_proxy(leg(strike="600", spot="720"), as_of=AS_OF) > 0.9


def test_deep_out_of_the_money_call_approaches_zero():
    assert delta_proxy(leg(strike="900", spot="720"), as_of=AS_OF) < 0.1


def test_delta_is_bounded_for_both_sides():
    for strike in ("100", "300", "720", "1200", "5000"):
        for side in ("CALL", "PUT"):
            value = delta_proxy(leg(side=side, strike=strike), as_of=AS_OF)
            assert -1.0 <= value <= 1.0


def test_monotone_in_strike_for_calls():
    deltas = [delta_proxy(leg(strike=str(k)), as_of=AS_OF) for k in (600, 700, 720, 800)]
    assert deltas == sorted(deltas, reverse=True)


def test_a_longer_dated_option_at_the_money_is_near_a_half_too():
    near = delta_proxy(leg(expiry=date(2026, 9, 2)), as_of=AS_OF)
    far = delta_proxy(leg(expiry=date(2026, 12, 18)), as_of=AS_OF)
    assert abs(near - 0.5) < 0.05 and abs(far - 0.5) < 0.05


# ── construction rules ────────────────────────────────────────────────────
def test_a_leg_must_be_well_formed():
    with pytest.raises(ExposureError):
        leg(side="STRADDLE")
    with pytest.raises(ExposureError):
        leg(contracts=0)
    with pytest.raises(ExposureError):
        leg(spot="0")


def test_limits_must_be_positive():
    with pytest.raises(ExposureError):
        Limits(max_gross_usd=D("0"))


# ── the book ──────────────────────────────────────────────────────────────
def test_a_single_ticket_is_within_default_limits():
    report = assess([leg()], as_of=AS_OF)
    assert report.ok and report.gross_usd > 0


def test_premium_at_risk_is_the_most_the_book_can_lose():
    report = assess([leg(contracts=2, premium="1.20")], as_of=AS_OF)
    assert report.risk_usd == pytest.approx(240.0)


def test_two_index_calls_are_more_correlated_than_two_single_names():
    index_pair = assess([leg("QQQ"), leg("SPY", spot="601", strike="600")], as_of=AS_OF)
    names = assess([leg("AAPL", spot="230", strike="230"),
                    leg("TSLA", spot="250", strike="250")], as_of=AS_OF)
    assert index_pair.correlated_usd > names.correlated_usd


def test_correlation_proxies_are_ordered():
    assert correlation("QQQ", "QQQ") == 1.0
    assert correlation("QQQ", "SPY") == CORRELATION["index_pair"]
    assert correlation("QQQ", "AAPL") == CORRELATION["cross"]


def test_a_concentrated_book_breaches_by_underlying():
    report = assess([leg(contracts=8)], as_of=AS_OF, limits=Limits(max_per_underlying_usd=D("50000")))
    assert any(b.rule == "max_per_underlying_usd" for b in report.breaches)
    assert any(b.detail == "QQQ" for b in report.breaches)


def test_same_expiry_concentration_is_its_own_breach():
    report = assess([leg(contracts=6)], as_of=AS_OF,
                    limits=Limits(max_same_expiry_usd=D("50000")))
    assert any(b.rule == "max_same_expiry_usd" for b in report.breaches)


def test_a_candidate_is_checked_before_the_order():
    fits_small, _ = candidate_fits([leg()], leg(contracts=1), as_of=AS_OF)
    fits_huge, breaches = candidate_fits([leg()], leg(contracts=40), as_of=AS_OF)
    assert fits_small is True
    assert fits_huge is False and breaches


def test_max_size_search_returns_a_size_that_fits():
    size = max_size_within([], leg(), as_of=AS_OF)
    assert size >= 1
    fits, _ = candidate_fits([], leg(contracts=size), as_of=AS_OF)
    assert fits is True


def test_max_size_is_zero_when_nothing_fits():
    impossible = Limits(max_per_underlying_usd=D("1"))
    assert max_size_within([], leg(), as_of=AS_OF, limits=impossible) == 0


def test_leg_exposure_exposes_notional_and_premium_separately():
    measured = leg_exposure(leg(contracts=2), as_of=AS_OF)
    assert measured.premium_usd == pytest.approx(240.0)
    assert measured.delta_usd > measured.premium_usd     # notional exceeds premium, always


def test_summary_names_the_worst_breach():
    report = assess([leg(contracts=40)], as_of=AS_OF)
    payload = summarise(report)
    assert payload["ok"] is False
    assert payload["worst_breach"]["over_by"] > 0


def test_render_is_operator_readable():
    assert "BREACH" in render(assess([leg(contracts=40)], as_of=AS_OF))


def test_positions_without_a_price_are_skipped_not_guessed():
    positions = {"QQQ|CALL|720|2026-09-01": type("P", (), {"quantity": 2})()}
    legs = from_positions(positions, spots={}, premiums={}, as_of=AS_OF)
    assert legs == []

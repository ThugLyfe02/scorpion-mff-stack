"""Roughness: the estimator recovers what it should, and refuses what the sample cannot support."""

import math
import random

import pytest

from scorpion.rough import (
    Estimate,
    RoughError,
    block_bootstrap,
    estimate,
    interval,
    lagged_moments,
    log_prices,
    render,
    report,
    vol_memory,
)


def walk(n=600, seed=1, drift=0.0):
    rng = random.Random(seed)
    price = 100.0
    out = [price]
    for _ in range(n):
        price *= math.exp(rng.gauss(drift, 0.01))
        out.append(price)
    return out


def reverting(n=600, seed=4):
    """Ornstein-Uhlenbeck: strong mean reversion, so the path is genuinely rough (low H)."""
    rng = random.Random(seed)
    price = 100.0
    out = [price]
    for _ in range(n):
        price += -0.9 * (price - 100.0) + rng.gauss(0.0, 0.4)
        out.append(price)
    return out


def trend(n=600):
    return [100.0 * math.exp(0.002 * i) for i in range(n)]


def test_a_random_walk_lands_near_one_half():
    est = estimate(walk(), lags=5)
    assert 0.3 < est.hurst < 0.7
    assert not est.identified or abs(est.hurst - 0.5) < 0.2


def test_a_trend_is_not_rough():
    assert estimate(trend(), lags=5).hurst > 0.7


def test_an_alternating_series_is_rough():
    assert estimate(reverting(), lags=5).hurst < 0.35


def test_non_positive_prices_are_refused():
    with pytest.raises(RoughError):
        log_prices([1.0, 0.0, 2.0, 3.0])


def test_a_thin_series_is_refused():
    with pytest.raises(RoughError):
        log_prices([1.0, 2.0])


def test_bad_lags_and_q_are_refused():
    with pytest.raises(RoughError):
        lagged_moments(walk(50), lags=1)
    with pytest.raises(RoughError):
        lagged_moments(walk(50), lags=3, q=0.0)


def test_moment_counts_shrink_with_the_lag():
    moments = lagged_moments(walk(100), lags=4)
    counts = [c for _lag, _m, c in moments]
    assert counts == sorted(counts, reverse=True)


def test_the_bootstrap_produces_a_bounded_interval():
    prices = walk(400, seed=7)
    samples = block_bootstrap(prices, lags=4, block=10, draws=60, seed=3)
    lo, hi = interval(samples)
    assert 0.0 <= lo < hi <= 1.5
    assert len(samples) == 60


def test_bad_interval_inputs_are_refused():
    with pytest.raises(RoughError):
        interval([])
    with pytest.raises(RoughError):
        interval([0.1, 0.2], level=1.5)


def test_an_unidentified_estimate_keeps_the_window_at_the_data_cap():
    # H = 0.50 with a wide standard error: indistinguishable from no roughness at all
    undecided = Estimate(hurst=0.50, intercept=0.0, stderr=0.20, lags=4, points=4)
    memory = vol_memory(undecided, max_bars=90)
    assert memory["bars"] == 90
    assert memory["identified"] is False
    assert "not identified" in memory["note"]


def test_an_identified_estimate_shortens_the_window():
    est = estimate(reverting(), lags=4)
    assert est.identified
    memory = vol_memory(est, max_bars=500)
    assert memory["bars"] < 500


def test_the_report_carries_caveats_and_renders():
    payload = report(walk(400, seed=5), lags=4, block=10, draws=40, seed=2)
    assert payload["caveats"]
    assert "memory" in payload
    assert "H =" in render(payload)

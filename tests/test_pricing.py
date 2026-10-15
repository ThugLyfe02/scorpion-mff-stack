"""Pricing rule tests - the two numbers the lock file fixes."""

from __future__ import annotations

import pytest

from scorpion_mff.pricing import ASK_OVER_BID_LIMIT, far_off_bid, marketable_limit, pct_off_bid


def test_limit_is_the_ask_when_the_ask_is_closer():
    # his bid 1.00 -> cap 1.15; ask 1.05 is below the cap, so the ask wins
    assert marketable_limit(1.00, 1.05) == 1.05


def test_limit_is_the_cap_when_the_ask_is_far():
    # ask 1.40 is above his bid * 1.15 = 1.15 -> the cap protects us
    assert marketable_limit(1.00, 1.40) == 1.15


def test_limit_is_floored_to_the_cent_never_rounded_up():
    # cap = 1.005 * 1.15 = 1.15575 -> 1.15, not 1.16: a limit one cent too
    # high breaks the rule the cap encodes.
    assert marketable_limit(1.005, 9.99) == 1.15


def test_limit_from_the_real_9_8_alert():
    # NVDA: his bid 2.63, marketable ask 2.72 -> min(2.72, 3.0245) = 2.72
    assert marketable_limit(2.63, 2.72) == 2.72


@pytest.mark.parametrize("ask,expected", [
    (1.25, False),      # exactly +25% is still inside the rule
    (1.2501, True),
    (1.30, True),
    (1.10, False),
])
def test_far_off_is_strictly_greater_than_25_percent(ask, expected):
    assert far_off_bid(1.00, ask) is expected


def test_pct_off_bid_is_reported_for_the_audit():
    assert pct_off_bid(2.00, 2.50) == pytest.approx(25.0)


@pytest.mark.parametrize("bid,ask", [(0, 1.0), (1.0, 0), (-1, 1.0)])
def test_nonsense_prices_raise_rather_than_guess(bid, ask):
    with pytest.raises(ValueError):
        marketable_limit(bid, ask)
    with pytest.raises(ValueError):
        far_off_bid(bid, ask)


def test_cap_constant_matches_the_lock_file():
    assert ASK_OVER_BID_LIMIT == 1.15

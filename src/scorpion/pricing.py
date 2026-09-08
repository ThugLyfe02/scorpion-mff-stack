from __future__ import annotations

from decimal import Decimal

from .config import DEFAULT_POLICY, Policy


def entry_limit(reference_bid: Decimal, live_ask: Decimal, policy: Policy = DEFAULT_POLICY) -> Decimal:
    if reference_bid <= 0 or live_ask <= 0:
        raise ValueError("prices must be positive")
    return min(live_ask, reference_bid * (Decimal("1") + policy.price_limit_markup))


def dislocation(reference_bid: Decimal, current_price: Decimal) -> Decimal:
    if reference_bid <= 0:
        raise ValueError("reference bid must be positive")
    return (current_price - reference_bid) / reference_bid


def entry_is_stale(reference_bid: Decimal, current_price: Decimal, policy: Policy = DEFAULT_POLICY) -> bool:
    return dislocation(reference_bid, current_price) > policy.max_dislocation_from_reference

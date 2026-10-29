"""The two locked PRICE rules. Pure arithmetic, no I/O.

live/RULES.lock.md:
    Limit: min(live_ask, his_bid * 1.15)
    Skip if already > +25% off his bid
"""

from __future__ import annotations

ASK_OVER_BID_LIMIT = 1.15      # marketable limit ceiling vs HIS posted bid
FAR_OFF_THRESHOLD = 1.25       # > +25% off his bid = do not chase


def marketable_limit(his_bid: float, live_ask: float) -> float:
    """min(live_ask, his_bid * 1.15), floored to the cent.

    Floored, not rounded: rounding up can exceed the cap the rule sets, and a
    limit one cent too high is a worse error than one cent low.
    """
    if his_bid <= 0:
        raise ValueError("his_bid must be positive")
    if live_ask <= 0:
        raise ValueError("live_ask must be positive")
    cap = his_bid * ASK_OVER_BID_LIMIT
    return _floor_cent(min(live_ask, cap))


def far_off_bid(his_bid: float, live_ask: float) -> bool:
    """True when the entry must be SKIPPED for being > +25% off his bid.

    Strictly greater: exactly +25% is still inside the rule (the lock says
    ">+25%"), and the boundary is nailed by a test. A missing ask (0) is
    nonsense input, not a cheap contract - it raises.
    """
    if his_bid <= 0:
        raise ValueError("his_bid must be positive")
    if live_ask <= 0:
        raise ValueError("live_ask must be positive")
    return live_ask > his_bid * FAR_OFF_THRESHOLD


def pct_off_bid(his_bid: float, live_ask: float) -> float:
    """How far the ask sits off his bid, in percent. For the audit log."""
    if his_bid <= 0:
        raise ValueError("his_bid must be positive")
    return (live_ask / his_bid - 1.0) * 100.0


def _floor_cent(value: float) -> float:
    return int(value * 100 + 1e-9) / 100

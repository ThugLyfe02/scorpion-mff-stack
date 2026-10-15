"""The locked clip table. Pure arithmetic - the money rules live here and only here.

live/RULES.lock.md + config/trader-profile.json:
    First trade of the day   = 1 contract (pipe test)
    After a clean pipe       = king / etf / platinum   $1500  (add $2250)
                               high-confidence-options $3000  (add $4500)
    Max concurrent positions = 2
    Add only if he posts added/avg, and only ONCE per position
"""

from __future__ import annotations

from enum import Enum

from .parser import channel_name

HIGH_CONFIDENCE_CHANNEL = "high-confidence-options"

BASE_CLIP_USD = 1500.0
ADD_CLIP_USD = 2250.0
HIGH_CONF_BASE_USD = 3000.0
HIGH_CONF_ADD_USD = 4500.0

MAX_CONCURRENT = 2
ADD_ALLOWED_PER_POSITION = 1
PIPE_TEST_CONTRACTS = 1


class Tier(str, Enum):
    PIPE = "pipe"                        # first trade of the day: 1 contract
    STANDARD = "standard"                # king / etf / platinum: $1500 / $2250
    HIGH_CONFIDENCE = "high_confidence"  # $3000 / $4500


def tier_for_channel(channel: str, is_first_of_day: bool) -> Tier:
    """Channel -> tier. The pipe test outranks the channel tier for trade #1."""
    if is_first_of_day:
        return Tier.PIPE
    return Tier.HIGH_CONFIDENCE if channel_name(channel) == HIGH_CONFIDENCE_CHANNEL else Tier.STANDARD


def clip_budget(tier: Tier, is_add: bool = False) -> float:
    """The premium budget for one clip, in USD."""
    if tier is Tier.STANDARD:
        return ADD_CLIP_USD if is_add else BASE_CLIP_USD
    if tier is Tier.HIGH_CONFIDENCE:
        return HIGH_CONF_ADD_USD if is_add else HIGH_CONF_BASE_USD
    if tier is Tier.PIPE:
        raise ValueError("the pipe test is a contract count, not a dollar budget")
    raise ValueError(f"unknown tier {tier!r}")


def contracts_for(budget_usd: float, limit_price: float, minimum: int = 1) -> int:
    """Whole contracts the budget buys at the limit price.

    Options are 100-share multipliers. Never returns 0 for a trade we decided to
    take: a clip that rounds to zero would silently drop the signal, which is
    the exact failure mode (silence) this package exists to fix. Minimum 0 when
    the caller passes minimum=0 (used to size an add against a tiny budget).
    """
    if limit_price <= 0:
        raise ValueError("limit_price must be positive")
    if budget_usd < 0:
        raise ValueError("budget_usd must not be negative")
    qty = int(budget_usd // (limit_price * 100))
    return max(minimum, qty)


def pipe_test_size() -> int:
    return PIPE_TEST_CONTRACTS

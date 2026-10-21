"""Deterministic clip sizing for the event-sourced control plane.

Before this module every effect was quantity-agnostic except the first entry of the day,
which carried the policy's pipe-test hint. A control plane that cannot size a trade is a
message router, so this is the arithmetic half of `live/RULES.lock.md`:

    first entry of the market day  1 contract (the pipe test - proves a fill is real)
    standard channels              $1,500 base / $2,250 on an explicit add
    high-confidence channel        $3,000 base / $4,500 on an explicit add

Every function here is pure and total over its documented domain: the same inputs always
produce the same integer, and an input that cannot be sized raises instead of guessing.
The rounding rule is the one that costs money if it is wrong: options are quoted per
contract with a 100-share multiplier, and a budget that buys 4.7 contracts buys **4**. A
clip that rounds up spends money that was never approved; a clip that rounds to zero
silently drops a signal the desk decided to take, so a decided trade is never zero - the
caller must either size it at 1 or refuse it explicitly, and `contracts_for` gives the
zero only when it was asked to (`minimum=0`).

Nothing here knows about risk. Risk can only cut a clip further (`risk.py`), never grow it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

CONTRACT_MULTIPLIER = Decimal("100")
HIGH_CONFIDENCE_CHANNEL_ID = "1448448931116748993"

DEFAULT_STANDARD_CLIP_USD = Decimal("1500")
DEFAULT_STANDARD_ADD_USD = Decimal("2250")
DEFAULT_HIGH_CONFIDENCE_CLIP_USD = Decimal("3000")
DEFAULT_HIGH_CONFIDENCE_ADD_USD = Decimal("4500")
DEFAULT_MAX_CONTRACTS_PER_CLIP = 50


class SizingError(ValueError):
    """An input that cannot be sized. Never swallowed: a size is a money decision."""


@dataclass(frozen=True, slots=True)
class ClipTable:
    """The dollar table from the locked rules, as configuration rather than constants."""

    standard_usd: Decimal = DEFAULT_STANDARD_CLIP_USD
    standard_add_usd: Decimal = DEFAULT_STANDARD_ADD_USD
    high_confidence_usd: Decimal = DEFAULT_HIGH_CONFIDENCE_CLIP_USD
    high_confidence_add_usd: Decimal = DEFAULT_HIGH_CONFIDENCE_ADD_USD
    max_contracts_per_clip: int = DEFAULT_MAX_CONTRACTS_PER_CLIP

    def __post_init__(self) -> None:
        for name in ("standard_usd", "standard_add_usd", "high_confidence_usd",
                     "high_confidence_add_usd"):
            value = getattr(self, name)
            if value <= 0:
                raise SizingError(f"{name} must be positive, got {value}")
        if self.max_contracts_per_clip < 1:
            raise SizingError("max_contracts_per_clip must be at least 1")
        if self.standard_add_usd < self.standard_usd:
            raise SizingError("an add clip must not be smaller than the base clip")
        if self.high_confidence_usd < self.standard_usd:
            raise SizingError("the high-confidence clip must not be smaller than standard")

    def budget_usd(self, *, high_confidence: bool, is_add: bool) -> Decimal:
        if high_confidence:
            return self.high_confidence_add_usd if is_add else self.high_confidence_usd
        return self.standard_add_usd if is_add else self.standard_usd


DEFAULT_CLIPS = ClipTable()


def is_high_confidence_channel(channel_id: str) -> bool:
    return channel_id == HIGH_CONFIDENCE_CHANNEL_ID


def contracts_for(
    budget_usd: Decimal,
    limit_price: Decimal,
    *,
    minimum: int = 1,
    maximum: int = DEFAULT_MAX_CONTRACTS_PER_CLIP,
) -> int:
    """Whole contracts a premium budget buys at a limit price.

    Raises rather than guessing when the inputs cannot produce a positive size:
    a non-positive limit price is a broken quote, and a negative budget is a bug.
    Returns 0 only when `minimum=0`, which is how a caller says "this position may be
    too small to add to" without being lied to.
    """
    if limit_price <= 0:
        raise SizingError(f"limit_price must be positive, got {limit_price}")
    if budget_usd < 0:
        raise SizingError(f"budget_usd must not be negative, got {budget_usd}")
    if minimum < 0:
        raise SizingError(f"minimum must not be negative, got {minimum}")
    if maximum < 1:
        raise SizingError(f"maximum must be at least 1, got {maximum}")
    if minimum > maximum:
        raise SizingError(f"minimum {minimum} exceeds maximum {maximum}")

    raw = (budget_usd / (limit_price * CONTRACT_MULTIPLIER)).to_integral_value(
        rounding=ROUND_FLOOR
    )
    sized = int(raw)
    if sized < minimum:
        return minimum
    if sized > maximum:
        return maximum
    return sized


def size_effect(
    *,
    quantity_hint: int | None,
    limit_price: Decimal,
    channel_id: str,
    is_add: bool,
    clips: ClipTable = DEFAULT_CLIPS,
) -> int:
    """The quantity for one proposed effect.

    A `quantity_hint` from policy (today: the pipe test) always wins: it is an explicit
    instruction to size at a fixed contract count, not a budget to be reinterpreted.
    Otherwise the channel's dollar clip buys as many whole contracts as the limit allows.
    """
    if quantity_hint is not None:
        if quantity_hint < 1:
            raise SizingError(f"quantity_hint must be positive, got {quantity_hint}")
        if quantity_hint > clips.max_contracts_per_clip:
            raise SizingError(
                f"quantity_hint {quantity_hint} exceeds max {clips.max_contracts_per_clip}"
            )
        return quantity_hint
    budget = clips.budget_usd(
        high_confidence=is_high_confidence_channel(channel_id), is_add=is_add
    )
    return contracts_for(budget, limit_price, maximum=clips.max_contracts_per_clip)

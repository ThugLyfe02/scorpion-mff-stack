from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from .broker import Quote, StaleQuote
from .config import DEFAULT_POLICY, Policy


def entry_limit(
    reference_bid: Decimal,
    live_ask: Decimal,
    policy: Policy = DEFAULT_POLICY,
) -> Decimal:
    if reference_bid <= 0 or live_ask <= 0:
        raise ValueError("prices must be positive")
    return min(live_ask, reference_bid * (Decimal("1") + policy.price_limit_markup))


def dislocation(reference_bid: Decimal, current_price: Decimal) -> Decimal:
    if reference_bid <= 0:
        raise ValueError("reference bid must be positive")
    return (current_price - reference_bid) / reference_bid


def entry_is_stale(
    reference_bid: Decimal,
    current_price: Decimal,
    policy: Policy = DEFAULT_POLICY,
) -> bool:
    return dislocation(reference_bid, current_price) > policy.max_dislocation_from_reference


_CENT = Decimal("0.01")
_SUB_DOLLAR_TICK = Decimal("0.0001")


def equity_tick(price: Decimal) -> Decimal:
    """Reg NMS Rule 612 minimum increment for a US equity limit price."""

    return _CENT if price >= Decimal("1") else _SUB_DOLLAR_TICK


def round_down_to_tick(price: Decimal) -> Decimal:
    tick = equity_tick(price)
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def guarded_entry_limit(
    quote: Quote | StaleQuote,
    reference_price: Decimal,
    *,
    max_chase_bps: int,
) -> Decimal | None:
    """Long buy limit for the paper stock trade: ``min(ask, ref * (1 + cap))``, tick-rounded down.

    Returns ``None`` (skip) when the quote is a :class:`StaleQuote` or unusable. It never prices
    up because data is stale; the worst data gets no order at all.
    """

    if isinstance(quote, StaleQuote):
        return None
    if reference_price <= 0 or quote.ask <= 0 or quote.bid <= 0 or quote.ask < quote.bid:
        return None
    if max_chase_bps < 0:
        raise ValueError("max_chase_bps cannot be negative")
    cap = reference_price * (Decimal("1") + Decimal(max_chase_bps) / Decimal("10000"))
    limit = round_down_to_tick(min(quote.ask, cap))
    return limit if limit > 0 else None


def reference_price(quote: Quote | StaleQuote) -> Decimal | None:
    """'Current price' for the first limit: last trade if inside the NBBO, else the midpoint."""

    if isinstance(quote, StaleQuote):
        return None
    if quote.last is not None and quote.bid <= quote.last <= quote.ask:
        return quote.last
    return (quote.bid + quote.ask) / Decimal("2")

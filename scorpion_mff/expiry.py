"""Expiry: what to do in the last fifteen minutes of a contract's life.

A 0DTE contract is not "an option with a short expiry". After the close it is cash or a
hundred shares per contract, and the market that price lived in stops existing. That makes
the final minutes a different market from the one the rest of the hot path trades:

  * the bid thins out, then stops being a price at all - so silence is never OTM;
  * on a half-day the close is 13:00 ET, and a 16:00 assumption leaves three hours of
    exposure nobody is watching;
  * an ISO expiry is a *date*, and the date is Eastern - at 01:30 UTC a 2025-09-09 contract
    is still Monday evening in New York, and a UTC comparison closes it a day early.

The verdict is deliberately three-valued rather than a boolean:

    HOLD            the contract is not expiring, or the cutoff has not started
    FORCE_CLOSE     inside the cutoff with a live bid - flatten now, at that bid
    HALT_AND_PAGE   a human must look: at/after the close, a dead bid, an unreadable key

`now` is accepted as an aware datetime or as epoch seconds, because the engine's clock is
epoch seconds and re-deriving the conversion in every caller is how a naive UTC assumption
gets in.

`HALT_AND_PAGE` is the value that costs money to get wrong in either direction. Paging on
every tick gets the bot muted; assuming the best leaves an assignment on the account at
09:30. So the rule is narrow: page only when the position is expiring and the machine
cannot prove what it is worth from data it actually has.

Two silences are never inferred into facts:

  * no bid (None, 0, negative, NaN) is *no market*, not a worthless contract;
  * an unknown underlying or strike is an *unknown* settlement, not an out-of-the-money
    one - `assignment_shares` returns None for exactly that, and 0 only for a price that
    was actually read and was actually on the wrong side of the strike.

Positions are long premium only (this stack buys calls and puts), so assignment shares are
signed by the contract: a long call exercised delivers stock to you, a long put delivers it
away.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from .calendar import Session, et_date, session_for
from .types import Position

SHARES_PER_CONTRACT = 100

# `parser._expiry` normalises "TODAY"/"ODTE"/"0DTE" to "0DTE"; accept the raw spellings too,
# because a hand-written key or an older tape must not turn a same-day contract into an
# unreadable one. This is a superset of the engine's exact `expiry.upper() == "0DTE"` test.
SAME_DAY_TOKENS = frozenset({"0DTE", "0-DTE", "ODTE", "TODAY"})

SIDES = frozenset({"CALL", "PUT"})

DEFAULT_CUTOFF_MINUTES = 15


class ExpiryAction(StrEnum):
    """What the expiry monitor should do. Not a boolean: `HOLD` is a decision too."""

    HOLD = "hold"
    FORCE_CLOSE = "force_close"
    HALT_AND_PAGE = "halt_and_page"


@dataclass(frozen=True, slots=True)
class ContractTerms:
    """The four readable fields of a contract key. `strike` is None when unreadable."""

    ticker: str
    side: str
    strike: float | None
    expiry: str

    @property
    def is_call(self) -> bool:
        return self.side == "CALL"

    @property
    def same_day(self) -> bool:
        return self.expiry.upper() in SAME_DAY_TOKENS


@dataclass(frozen=True, slots=True)
class ExpiryVerdict:
    """An action plus the evidence for it. The page is only as good as the evidence."""

    action: ExpiryAction
    reason: str
    contract_key: str
    contracts: int
    expiry: date | None
    session: Session | None
    close_utc: datetime | None
    minutes_to_close: float | None
    settlement: str       # "itm" | "at_the_money" | "out_of_the_money" | "unknown" | "none"
    assignment_shares: int | None

    @property
    def must_page(self) -> bool:
        return self.action is ExpiryAction.HALT_AND_PAGE

    @property
    def must_flatten(self) -> bool:
        return self.action is ExpiryAction.FORCE_CLOSE

    @property
    def actionable(self) -> bool:
        return self.action is not ExpiryAction.HOLD

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "contract_key": self.contract_key,
            "contracts": self.contracts,
            "expiry": self.expiry.isoformat() if self.expiry is not None else None,
            "close_utc": self.close_utc.isoformat() if self.close_utc is not None else None,
            "minutes_to_close": self.minutes_to_close,
            "settlement": self.settlement,
            "assignment_shares": self.assignment_shares,
        }


def _strike(raw: str) -> float | None:
    """A strike that was actually written down. 'nan', '0' and '?' are not strikes."""
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def contract_terms(contract_key: str) -> ContractTerms | None:
    """Split `TICKER|SIDE|STRIKE|EXPIRY`, or None when the key is not that shape.

    The side is validated here because a key without one is not a position this stack can
    hold, and the strike is allowed to be unreadable: an unreadable strike still carries a
    readable expiry, which is enough to know when to stop holding it.
    """
    parts = (contract_key or "").strip().split("|")
    if len(parts) != 4:
        return None
    ticker, side, strike, expiry = (part.strip() for part in parts)
    side = side.upper()
    if not ticker or side not in SIDES:
        return None
    return ContractTerms(
        ticker=ticker.upper(), side=side, strike=_strike(strike), expiry=expiry
    )


def expiry_day(contract_key: str) -> date | None:
    """The ISO expiry encoded in a key, or None. `0DTE` is relative, so it is not a date."""
    terms = contract_terms(contract_key)
    if terms is None:
        return None
    try:
        return date.fromisoformat(terms.expiry)
    except ValueError:
        return None


def resolve_expiry(contract_key: str, today: date) -> date | None:
    """The calendar date a key expires on, resolving `0DTE` against today's session date."""
    terms = contract_terms(contract_key)
    if terms is None:
        return None
    return today if terms.same_day else expiry_day(contract_key)


def instant(now: datetime | float) -> datetime:
    """One adapter for the two clocks this stack actually has.

    The engine's clock is epoch seconds (`ts: float`); the calendar speaks aware
    datetimes. Converting at this boundary beats every caller re-deriving it, and a float
    is unambiguously UTC, so it cannot smuggle in a naive local time. A datetime is passed
    through untouched: a naive one is still refused, loudly, by the calendar.
    """
    if isinstance(now, datetime):
        return now
    seconds = float(now)
    if not math.isfinite(seconds):
        raise ValueError(f"now must be a finite instant, not {seconds!r}")
    return datetime.fromtimestamp(seconds, tz=UTC)


def is_live_bid(bid: float | None) -> bool:
    """A bid that can actually take the other side: present, finite, positive.

    Zero is not a market - it is the absence of one with a number attached - and NaN is a
    transmission artefact. Treating either as a fill price is how a "closed" position stays
    open, or a healthy one is liquidated at nothing.
    """
    return bid is not None and math.isfinite(bid) and bid > 0


def assignment_shares(
    contract_key: str, contracts: int, underlying_close: float | None
) -> int | None:
    """Signed shares delivered if this long option expires in the money.

    A long call that finishes above its strike buys 100 shares per contract (+); a long put
    that finishes below it sells 100 per contract (-). The return is None - "cannot
    compute", never "zero" - when the strike or the underlying is unknown, because a guess
    here understates overnight risk.

    A known price on the wrong side of the strike is 0: an honestly worthless contract.
    Exactly at the strike is also 0, and is *not* safe - a pin is decided after the close,
    which is why the verdict still pages a position that is still open when the bell rings.
    """
    terms = contract_terms(contract_key)
    if terms is None or terms.strike is None or underlying_close is None:
        return None
    if not math.isfinite(underlying_close):
        return None
    if contracts <= 0:
        return 0
    if terms.is_call:
        in_the_money = underlying_close > terms.strike
        sign = 1
    else:
        in_the_money = underlying_close < terms.strike
        sign = -1
    return sign * SHARES_PER_CONTRACT * contracts if in_the_money else 0


def _settlement(
    terms: ContractTerms | None, contracts: int, underlying: float | None,
    shares: int | None,
) -> str:
    """Name what the account faces at expiry - and say "unknown" when it is unknown."""
    if contracts <= 0:
        return "none"
    if terms is None or terms.strike is None or underlying is None or shares is None:
        return "unknown"
    if shares != 0:
        return "itm"
    return "at_the_money" if underlying == terms.strike else "out_of_the_money"


def verdict_detail(
    now: datetime | float,
    contract_key: str,
    contracts: int,
    bid: float | None,
    underlying: float | None,
    cutoff_minutes: int = DEFAULT_CUTOFF_MINUTES,
) -> ExpiryVerdict:
    """The full expiry verdict for one position: action, reason, and its evidence.

    Order matters, and it is the order a real session presents:

      1. nothing open                    -> HOLD (nothing to do, whatever the date says)
      2. key/expiry unreadable           -> HALT_AND_PAGE (never guess a contract's life)
      3. expires later than today        -> HOLD (the cutoffs below do not apply yet)
      4. expires before today            -> HALT_AND_PAGE (a phantom position needs a human)
      5. expiry day: before the open     -> HOLD (a bid at 04:00 ET is yesterday's)
      6. at/after the close              -> HALT_AND_PAGE (it is cash or shares now)
      7. before the cutoff window        -> HOLD
      8. inside the cutoff, live bid     -> FORCE_CLOSE (at that bid, not a guess)
      9. inside the cutoff, no live bid  -> HALT_AND_PAGE (silence is not OTM)

    `underlying` never changes the action - the close decides that - it only lets the page
    state assignment exposure when it is knowable, and refuses to state it when it is not.
    """
    if cutoff_minutes < 0:
        raise ValueError(f"cutoff_minutes must be >= 0, not {cutoff_minutes}")
    moment = instant(now)
    day = et_date(moment)
    terms = contract_terms(contract_key)
    shares = assignment_shares(contract_key, contracts, underlying)
    settlement = _settlement(terms, contracts, underlying, shares)

    def make(
        action: ExpiryAction,
        reason: str,
        expiry: date | None = None,
        session: Session | None = None,
    ) -> ExpiryVerdict:
        return ExpiryVerdict(
            action=action,
            reason=reason,
            contract_key=contract_key,
            contracts=contracts,
            expiry=expiry,
            session=session,
            close_utc=session.close_utc if session is not None else None,
            minutes_to_close=(
                round(session.minutes_to_close(moment), 1) if session is not None else None
            ),
            settlement=settlement,
            assignment_shares=shares,
        )

    if contracts <= 0:
        return make(ExpiryAction.HOLD, "no_open_position")
    if terms is None:
        return make(ExpiryAction.HALT_AND_PAGE, "contract_key_unparsed")

    if terms.same_day:
        expiry = day
    else:
        parsed = expiry_day(contract_key)
        if parsed is None:
            return make(ExpiryAction.HALT_AND_PAGE, "expiry_unparsed")
        if parsed > day:
            return make(ExpiryAction.HOLD, "not_expiring", expiry=parsed)
        if parsed < day:
            # The contract provably cannot exist. An open position that still names it is a
            # reconciliation failure: release it on a guess and the slot under-counts risk.
            return make(ExpiryAction.HALT_AND_PAGE, "expired_unreconciled", expiry=parsed)
        expiry = parsed

    session = session_for(expiry)            # raises CalendarError for an unknown year
    if session is None:
        return make(ExpiryAction.HALT_AND_PAGE, "expiry_without_session", expiry=expiry)
    if moment < session.open_utc:
        return make(ExpiryAction.HOLD, "pre_open", expiry=expiry, session=session)
    if moment >= session.close_utc:
        return make(ExpiryAction.HALT_AND_PAGE, "at_or_after_close", expiry=expiry,
                    session=session)
    cutoff_start = session.cutoff_utc(cutoff_minutes)
    if moment < cutoff_start:
        return make(ExpiryAction.HOLD, "before_cutoff", expiry=expiry, session=session)
    if not is_live_bid(bid):
        return make(ExpiryAction.HALT_AND_PAGE, "cutoff_without_live_bid", expiry=expiry,
                    session=session)
    return make(ExpiryAction.FORCE_CLOSE, "cutoff_with_live_bid", expiry=expiry,
                session=session)


def expiry_verdict(
    now: datetime | float,
    contract_key: str,
    contracts: int,
    bid: float | None,
    underlying: float | None,
    cutoff_minutes: int = DEFAULT_CUTOFF_MINUTES,
) -> ExpiryAction:
    """The action alone. Derived from `verdict_detail`, so the two cannot disagree."""
    return verdict_detail(
        now, contract_key, contracts, bid, underlying, cutoff_minutes
    ).action


_URGENCY: dict[ExpiryAction, int] = {
    ExpiryAction.HALT_AND_PAGE: 0,
    ExpiryAction.FORCE_CLOSE: 1,
    ExpiryAction.HOLD: 2,
}


def sweep(
    now: datetime | float,
    positions: Iterable[Position],
    bids: Mapping[str, float],
    underlying: Mapping[str, float],
    cutoff_minutes: int = DEFAULT_CUTOFF_MINUTES,
) -> tuple[ExpiryVerdict, ...]:
    """Every actionable expiry verdict in the book - the ones needing a human first.

    `underlying` is keyed by ticker ("QQQ"), because a book holds more than one. A position
    that is not expiring produces HOLD and is omitted: a monitor that reports everything
    reports nothing, and the caller already has the book.

    Ordering is the point of the tuple: a page outranks a flatten, and inside each the
    contract closest to (or furthest past) its close comes first.
    """
    verdicts: list[ExpiryVerdict] = []
    for position in positions:
        if position.contracts <= 0:
            continue
        terms = contract_terms(position.contract_key)
        ticker = terms.ticker if terms is not None else position.ticker
        verdict = verdict_detail(
            now,
            position.contract_key,
            position.contracts,
            bids.get(position.contract_key),
            underlying.get(ticker),
            cutoff_minutes,
        )
        if verdict.actionable:
            verdicts.append(verdict)
    return tuple(
        sorted(
            verdicts,
            key=lambda v: (_URGENCY[v.action], v.minutes_to_close or 0.0),
        )
    )


def page_text(verdict: ExpiryVerdict) -> str:
    """One line a human can act on. It never claims a value the machine does not have."""
    expiry = verdict.expiry.isoformat() if verdict.expiry is not None else "?"
    parts = [
        f"{verdict.action.value.upper()}: {verdict.contract_key}",
        f"{verdict.contracts} contract(s), expiry {expiry}",
    ]
    if verdict.minutes_to_close is not None:
        parts.append(f"{verdict.minutes_to_close:+.1f} min to close")
    parts.append(f"reason={verdict.reason}")
    if verdict.settlement == "itm" and verdict.assignment_shares:
        parts.append(f"assignment {verdict.assignment_shares:+d} shares")
    elif verdict.settlement == "unknown":
        parts.append("settlement unknown - a missing price is not OTM")
    return " | ".join(parts)


__all__ = [
    "DEFAULT_CUTOFF_MINUTES",
    "SAME_DAY_TOKENS",
    "SHARES_PER_CONTRACT",
    "ContractTerms",
    "ExpiryAction",
    "ExpiryVerdict",
    "assignment_shares",
    "contract_terms",
    "expiry_day",
    "expiry_verdict",
    "instant",
    "is_live_bid",
    "page_text",
    "resolve_expiry",
    "sweep",
    "verdict_detail",
]

"""Exposure: a book is not a count of positions, it is a set of correlated bets.

`max_open_positions=2` counts tickets. Two QQQ calls and a SPY put are three tickets and roughly one
bet; a QQQ call and a TSLA call are two bets. The cap that counts tickets therefore permits
concentration precisely when it matters and forbids diversification precisely when it does not.

This module measures the book the way risk is actually carried:

* **delta-equivalent exposure per position**, derived from moneyness rather than a greeks
  feed. It is a proxy and is labelled one - a long option's delta is bounded in (0, 1) for
  calls and (-1, 0) for puts, rises with moneyness, and decays toward zero out of the money.
  The shape is a logistic in log-moneyness with a scale that widens with time to expiry. It is
  wrong in the third decimal and right in the first, which is the precision a cap needs.
* **correlation by underlying**, because exposure to two index products is not two exposures. The
  proxy is deliberately crude: same underlying is 1.0, both index ETFs is a high constant, otherwise
  a low constant - and the report says so. A covariance matrix estimated from six bar sets would be
  false precision.
* **expiry clustering**, because a book whose positions all expire at the same close is one event,
  not several.
* **breach reporting with the rule that broke**, so a refusal is diagnosable, in the same style as
  the authorization ladder.

The output is a report, not a decision: `assess()` never mutates the book and never refuses. The
caller decides, which keeps the risk opinion auditable and keeps this module usable by research as
well as by the executor.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

D = Decimal

# Correlation proxies. Deliberately coarse: an estimated matrix from this data would be false
# precision, and a cap only needs to know "these move together" versus "these do not".
INDEX_ETFS = frozenset({"SPY", "QQQ", "IWM", "DIA", "SPLG", "VOO", "VTI"})
CORRELATION = {
    "same": 1.0,
    "index_pair": 0.85,      # two broad index ETFs
    "cross": 0.35,           # different single names, or index vs single name
}


class ExposureError(ValueError):
    """A book that cannot be measured, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Limits:
    """What the book is allowed to carry. Values are in delta-equivalent dollars."""

    # Delta-equivalent dollars are NOTIONAL, not premium: a 2-lot index call at a 720 spot carries
    # roughly $72k of notional while risking $240. A cap denominated in notional therefore has to be
    # an order of magnitude larger than the premium the desk risks, or the first ticket breaches it.
    max_gross_usd: D = D("150000")
    max_net_usd: D = D("100000")
    max_per_underlying_usd: D = D("80000")
    max_same_expiry_usd: D = D("120000")
    max_correlated_usd: D = D("120000")

    def __post_init__(self) -> None:
        for name in ("max_gross_usd", "max_net_usd", "max_per_underlying_usd",
                     "max_same_expiry_usd", "max_correlated_usd"):
            if getattr(self, name) <= 0:
                raise ExposureError("bad_limit", f"{name} must be positive")

    def to_dict(self) -> dict[str, str]:
        return {
            "max_gross_usd": str(self.max_gross_usd), "max_net_usd": str(self.max_net_usd),
            "max_per_underlying_usd": str(self.max_per_underlying_usd),
            "max_same_expiry_usd": str(self.max_same_expiry_usd),
            "max_correlated_usd": str(self.max_correlated_usd),
        }


@dataclass(frozen=True, slots=True)
class Leg:
    """One held contract, with the few facts the proxy needs."""

    ticker: str
    side: str                 # CALL | PUT
    strike: D
    expiry: date
    contracts: int
    premium: D                # last known price per contract
    spot: D                   # underlying last, used for moneyness

    def __post_init__(self) -> None:
        if self.side not in ("CALL", "PUT"):
            raise ExposureError("bad_side", self.side)
        if self.contracts <= 0:
            raise ExposureError("bad_contracts", str(self.contracts))
        if self.spot <= 0 or self.strike <= 0:
            raise ExposureError("bad_price", f"spot {self.spot} strike {self.strike}")

    @property
    def key(self) -> str:
        return f"{self.ticker}|{self.side}|{self.strike}|{self.expiry.isoformat()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "ticker": self.ticker, "side": self.side,
            "strike": str(self.strike), "expiry": self.expiry.isoformat(),
            "contracts": self.contracts, "premium": str(self.premium), "spot": str(self.spot),
        }


def time_to_expiry_years(expiry: date, as_of: date) -> float:
    """Calendar days, floored at a day: an expiring contract still has today's risk."""
    days = max(1, (expiry - as_of).days)
    return days / 365.0


def delta_proxy(leg: Leg, *, as_of: date) -> float:
    """A long option's delta, estimated from moneyness. Bounded, monotone, expiry-aware.

    Calls: bounded in (0, 1). Puts: bounded in (-1, 0). At the money, |delta| ~ 0.5 and rises with
    time to expiry because a longer-dated option is more likely to finish in the money. This is a
    logistic in log-moneyness - the standard shape - with a scale of 1/sqrt(T).
    """
    t = time_to_expiry_years(leg.expiry, as_of)
    moneyness = math.log(float(leg.spot) / float(leg.strike))
    # 0.5*sqrt(T) is the standard shape. For same-day contracts T is 1/365, so the floor is
    # what decides deep-moneyness behaviour: at 0.15 a 20%-ITM 0DTE reads 0.77 delta when a
    # real one is ~0.99, which would understate every concentrated book.
    scale = max(0.03, 0.5 * math.sqrt(t))
    z = moneyness / scale
    call = 1.0 / (1.0 + math.exp(-z))
    if leg.side == "CALL":
        return call
    return -(1.0 - call)


@dataclass(frozen=True, slots=True)
class LegExposure:
    leg: Leg
    delta: float
    delta_usd: float          # delta * spot * contracts * 100
    premium_usd: float        # premium * contracts * 100 - what is at risk in full
    days_to_expiry: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.leg.key, "ticker": self.leg.ticker, "side": self.leg.side,
            "contracts": self.leg.contracts,
            "delta": round(self.delta, 4),
            "delta_usd": round(self.delta_usd, 2),
            "premium_usd": round(self.premium_usd, 2),
            "days_to_expiry": self.days_to_expiry,
        }


def leg_exposure(leg: Leg, *, as_of: date) -> LegExposure:
    delta = delta_proxy(leg, as_of=as_of)
    multiplier = 100
    return LegExposure(
        leg=leg, delta=delta,
        delta_usd=delta * float(leg.spot) * leg.contracts * multiplier,
        premium_usd=float(leg.premium) * leg.contracts * multiplier,
        days_to_expiry=max(0, (leg.expiry - as_of).days),
    )


def correlation(a: str, b: str) -> float:
    if a == b:
        return CORRELATION["same"]
    if a in INDEX_ETFS and b in INDEX_ETFS:
        return CORRELATION["index_pair"]
    return CORRELATION["cross"]


@dataclass(frozen=True, slots=True)
class Breach:
    rule: str
    measured: float
    limit: float
    detail: str = ""

    @property
    def over_by(self) -> float:
        return round(self.measured - self.limit, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule, "measured": round(self.measured, 2),
            "limit": round(self.limit, 2), "over_by": self.over_by, "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ExposureReport:
    as_of: date
    legs: tuple[LegExposure, ...]
    gross_usd: float
    net_usd: float
    by_underlying: dict[str, float]
    by_expiry: dict[str, float]
    correlated_usd: float
    breaches: tuple[Breach, ...]
    limits: Limits

    @property
    def ok(self) -> bool:
        return not self.breaches

    @property
    def risk_usd(self) -> float:
        """Total premium at risk: the most the book can lose if every contract expires worthless."""
        return round(sum(leg.premium_usd for leg in self.legs), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "ok": self.ok,
            "gross_usd": round(self.gross_usd, 2),
            "net_usd": round(self.net_usd, 2),
            "risk_usd": self.risk_usd,
            "correlated_usd": round(self.correlated_usd, 2),
            "by_underlying": {k: round(v, 2) for k, v in sorted(self.by_underlying.items())},
            "by_expiry": {k: round(v, 2) for k, v in sorted(self.by_expiry.items())},
            "legs": [leg.to_dict() for leg in self.legs],
            "breaches": [b.to_dict() for b in self.breaches],
            "limits": limits_to_dict(self.limits),
        }


def limits_to_dict(limits: Limits) -> dict[str, str]:
    return limits.to_dict()


def assess(
    legs: Sequence[Leg], *, as_of: date, limits: Limits | None = None,
) -> ExposureReport:
    """Measure the book. Pure: no mutation, no refusal, no side effects."""
    limits = limits or Limits()
    measured = tuple(leg_exposure(leg, as_of=as_of) for leg in legs)

    gross = sum(abs(m.delta_usd) for m in measured)
    net = sum(m.delta_usd for m in measured)

    by_underlying: dict[str, float] = {}
    by_expiry: dict[str, float] = {}
    for m in measured:
        by_underlying[m.leg.ticker] = by_underlying.get(m.leg.ticker, 0.0) + m.delta_usd
        key = m.leg.expiry.isoformat()
        by_expiry[key] = by_expiry.get(key, 0.0) + abs(m.delta_usd)

    # Correlated exposure: for each pair, the shared component weighted by the correlation proxy.
    # A book of two names at 0.35 correlation does not carry the same risk as two at 0.85, and the
    # cap has to know which it is looking at.
    correlated = 0.0
    for i, a in enumerate(measured):
        for b in measured[i + 1:]:
            w = correlation(a.leg.ticker, b.leg.ticker)
            correlated += w * min(abs(a.delta_usd), abs(b.delta_usd))
    correlated += sum(abs(m.delta_usd) for m in measured if m.leg.ticker in INDEX_ETFS) * 0.0

    breaches: list[Breach] = []
    if gross > float(limits.max_gross_usd):
        breaches.append(Breach("max_gross_usd", gross, float(limits.max_gross_usd)))
    if abs(net) > float(limits.max_net_usd):
        breaches.append(Breach("max_net_usd", abs(net), float(limits.max_net_usd),
                               "directional exposure"))
    for ticker, value in by_underlying.items():
        if abs(value) > float(limits.max_per_underlying_usd):
            breaches.append(Breach(
                "max_per_underlying_usd", abs(value), float(limits.max_per_underlying_usd),
                ticker,
            ))
    for expiry, value in by_expiry.items():
        if value > float(limits.max_same_expiry_usd):
            breaches.append(Breach(
                "max_same_expiry_usd", value, float(limits.max_same_expiry_usd), expiry,
            ))
    if correlated > float(limits.max_correlated_usd):
        breaches.append(Breach("max_correlated_usd", correlated,
                               float(limits.max_correlated_usd)))

    return ExposureReport(
        as_of=as_of, legs=measured, gross_usd=gross, net_usd=net,
        by_underlying=by_underlying, by_expiry=by_expiry, correlated_usd=correlated,
        breaches=tuple(breaches), limits=limits,
    )


def candidate_fits(
    legs: Sequence[Leg], candidate: Leg, *, as_of: date, limits: Limits | None = None,
) -> tuple[bool, list[Breach]]:
    """Would adding this leg breach a limit? Asked BEFORE the order, which is when it matters."""
    limits = limits or Limits()
    after = assess(list(legs) + [candidate], as_of=as_of, limits=limits)
    return (not after.breaches, list(after.breaches))


def max_size_within(
    legs: Sequence[Leg], template: Leg, *, as_of: date, limits: Limits | None = None,
    ceiling: int = 200,
) -> int:
    """The largest contract count that fits every limit, by search from the ceiling down.

    A closed form would need to invert five constraints with a correlation term; a bounded search
    is exact, obviously correct, and runs in microseconds at these sizes.
    """
    for size in range(ceiling, 0, -1):
        candidate = Leg(
            ticker=template.ticker, side=template.side, strike=template.strike,
            expiry=template.expiry, contracts=size, premium=template.premium, spot=template.spot,
        )
        fits, _ = candidate_fits(legs, candidate, as_of=as_of, limits=limits)
        if fits:
            return size
    return 0


def summarise(report: ExposureReport) -> dict[str, Any]:
    """The three numbers a desk reads before anything else."""
    return {
        "ok": report.ok,
        "gross_usd": round(report.gross_usd, 2),
        "net_usd": round(report.net_usd, 2),
        "risk_usd": report.risk_usd,
        "worst_breach": (
            max(report.breaches, key=lambda b: b.over_by).to_dict() if report.breaches else None
        ),
        "legs": len(report.legs),
        "underlyings": sorted(report.by_underlying),
    }


def render(report: ExposureReport) -> str:
    lines = [
        f"as of {report.as_of.isoformat()}  {len(report.legs)} legs  "
        f"{'OK' if report.ok else 'BREACH'}",
        f"  gross {report.gross_usd:>12,.2f}   net {report.net_usd:>+12,.2f}   "
        f"premium at risk {report.risk_usd:>12,.2f}",
        f"  correlated {report.correlated_usd:>9,.2f} of limit {report.limits.max_correlated_usd}",
    ]
    for ticker, value in sorted(report.by_underlying.items()):
        lines.append(f"    {ticker:6s} {value:>+12,.2f}")
    for breach in report.breaches:
        lines.append(
            f"  ! {breach.rule} {breach.measured:,.2f} > {breach.limit:,.2f} "
            f"({breach.detail})"
        )
    return "\n".join(lines)


def from_positions(
    positions: Mapping[str, Any], *, spots: Mapping[str, D], premiums: Mapping[str, D],
    as_of: date,
) -> list[Leg]:
    """Build legs from the book's position keys (`TICKER|SIDE|STRIKE|EXPIRY`).

    A position the caller cannot price is skipped rather than guessed at, and the omission is the
    caller's to notice - `assess` cannot report on what it was not given.
    """
    legs: list[Leg] = []
    for key, position in positions.items():
        parts = str(key).split("|")
        if len(parts) != 4:
            continue
        ticker, side, strike_raw, expiry_raw = parts
        quantity = int(getattr(position, "quantity", 0) or 0)
        if quantity <= 0:
            continue
        spot = spots.get(ticker)
        premium = premiums.get(str(key))
        if spot is None or premium is None:
            continue
        try:
            expiry = date.fromisoformat(expiry_raw)
            strike = D(strike_raw)
        except (ValueError, ArithmeticError):
            continue
        normalised = side.upper().rstrip("S")
        if normalised not in ("CALL", "PUT"):
            continue
        legs.append(Leg(ticker=ticker, side=normalised, strike=strike, expiry=expiry,
                        contracts=quantity, premium=premium, spot=spot))
    return legs

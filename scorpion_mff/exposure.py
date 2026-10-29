"""Correlated-book guard: two index calls are not two independent bets.

The locked rules cap the *number* of concurrent positions (two). They say nothing about
whether those two positions are one bet wearing two names. Two QQQ calls, or a QQQ call
and a QQQ put in the same expiry, are not two independent decisions - they are one bet,
doubled. This module is the cap a count alone cannot express:

* **per-underlying concentration** - refuse a second position in the same underlying,
  whatever the side or strike;
* **expiry clustering** - refuse when a new position would crowd an expiry day beyond
  the limit;
* **gross-premium cap per underlying** - refuse when the premium already at risk in one
  underlying, plus the incoming premium, exceeds a dollar cap.

The premium cap uses premium actually paid (contracts x avg_price x 100), not a greek.
That is deliberate: this desk has no greek engine, and a strike-distance delta proxy
would be a model pretending to be a measurement. Premium at risk is a fact the book
already knows.

The guard is a pure function of (limits, open book, signal, incoming size), so every
refusal can be replayed and explained from the fill log. It is OFF until wired: the
engine holds `exposure_limits=None`, and `arm` loads `live/exposure.json` when that file
exists. The capability ships tested; the activation is an explicit operator decision.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import Fill, Position, Side, Signal


def premium_usd(position: Position) -> float:
    """Premium at risk in one position, in dollars (contracts x avg price x 100)."""
    return position.contracts * position.avg_price * 100.0


def expiry_of(contract_key: str) -> str:
    """The expiry segment of a contract key: 'QQQ|CALL|600|0DTE' -> '0DTE'."""
    return contract_key.rsplit("|", 1)[-1] if "|" in contract_key else contract_key


@dataclass(frozen=True)
class ExposureLimits:
    max_per_underlying: int = 1
    max_per_expiry: int = 2
    max_premium_per_underlying_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_per_underlying": self.max_per_underlying,
            "max_per_expiry": self.max_per_expiry,
            "max_premium_per_underlying_usd": self.max_premium_per_underlying_usd,
        }


@dataclass(frozen=True)
class ExposureDecision:
    allowed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason}


def guard(
    limits: ExposureLimits,
    positions: Iterable[Position],
    signal: Signal,
    *,
    incoming_qty: int = 0,
    incoming_price: float = 0.0,
) -> ExposureDecision:
    """Decide whether `signal` may open against the book already held.

    Count-based checks run first (they need only the signal and the book); the premium
    cap runs last because it needs the sized quantity and limit, which only exist after
    the engine has priced the entry.
    """
    open_positions = [p for p in positions if p.is_open]

    underlying = signal.ticker.casefold()
    same_underlying = [p for p in open_positions if p.ticker.casefold() == underlying]
    if len(same_underlying) >= limits.max_per_underlying:
        return ExposureDecision(
            False,
            f"underlying:{signal.ticker or '?'}:{len(same_underlying)} open "
            f"(cap {limits.max_per_underlying})",
        )

    expiry = signal.expiry or expiry_of(signal.contract_key)
    same_expiry = [p for p in open_positions if expiry_of(p.contract_key) == expiry]
    if len(same_expiry) >= limits.max_per_expiry:
        return ExposureDecision(
            False,
            f"expiry:{expiry or '?'}:{len(same_expiry)} open (cap {limits.max_per_expiry})",
        )

    cap = limits.max_premium_per_underlying_usd
    if cap is not None and incoming_qty > 0:
        held = sum(premium_usd(p) for p in same_underlying)
        incoming = incoming_qty * incoming_price * 100.0
        if held + incoming > cap:
            return ExposureDecision(
                False,
                f"premium:{signal.ticker or '?'}:{held + incoming:,.0f} > cap {cap:,.0f}",
            )

    return ExposureDecision(True, "within_limits")


def book_exposure(positions: Iterable[Position]) -> dict[str, Any]:
    """Gross/net premium and concentration of the open book, per underlying and expiry.

    `net_premium_usd` is calls minus puts: a coarse direction, not a delta. It is
    labelled as premium, not exposure, so it cannot be mistaken for a greek number.
    """
    open_positions = [p for p in positions if p.is_open]
    per_underlying: dict[str, dict[str, Any]] = {}
    per_expiry: dict[str, dict[str, Any]] = {}
    gross = 0.0
    net = 0.0

    for position in open_positions:
        premium = premium_usd(position)
        ticker = position.ticker.casefold() or position.contract_key
        row = per_underlying.setdefault(
            ticker, {"positions": 0, "contracts": 0, "premium_usd": 0.0,
                     "calls": 0, "puts": 0})
        row["positions"] += 1
        row["contracts"] += position.contracts
        row["premium_usd"] = round(row["premium_usd"] + premium, 2)
        row["calls" if position.side.is_call else "puts"] += position.contracts

        expiry = expiry_of(position.contract_key)
        expiry_row = per_expiry.setdefault(
            expiry, {"positions": 0, "contracts": 0, "premium_usd": 0.0})
        expiry_row["positions"] += 1
        expiry_row["contracts"] += position.contracts
        expiry_row["premium_usd"] = round(expiry_row["premium_usd"] + premium, 2)

        gross += premium
        net += premium if position.side.is_call else -premium

    return {
        "positions": len(open_positions),
        "gross_premium_usd": round(gross, 2),
        "net_premium_usd": round(net, 2),
        "per_underlying": per_underlying,
        "per_expiry": per_expiry,
    }


def positions_from_fills(fills: Iterable[Fill]) -> list[Position]:
    """Rebuild the open book from the append-only fill log, for inspection only.

    The live book is the engine's memory. This is a read-only reconstruction of it from
    the one log both agree on, so `exposure show` can answer "what is the book holding
    right now" without a second source of truth. Buys (new or add) are weighted-average
    priced; sells and expires reduce; a zeroed position is dropped.
    """
    book: dict[str, Position] = {}
    for fill in sorted(fills, key=lambda item: item.seq):
        if not fill.contract_key:
            continue
        parts = fill.contract_key.split("|")
        if len(parts) < 2 or parts[1] not in {"CALL", "PUT"}:
            continue

        if fill.event == "buy" and fill.accepted:
            position = book.get(fill.contract_key)
            if position is None:
                position = Position(
                    contract_key=fill.contract_key,
                    ticker=parts[0],
                    side=Side(parts[1]),
                    contracts=0,
                    avg_price=0.0,
                    opened_ts=fill.ts,
                )
                book[fill.contract_key] = position
            total = position.contracts + fill.qty
            if total > 0:
                position.avg_price = (
                    position.avg_price * position.contracts + fill.price * fill.qty
                ) / total
            position.contracts = total
        elif fill.event in {"sell", "expire"}:
            position = book.get(fill.contract_key)
            if position is not None:
                position.contracts = max(0, position.contracts - fill.qty)
                if position.contracts == 0:
                    del book[fill.contract_key]

    return [position for position in book.values() if position.is_open]


def load_limits(path: str | os.PathLike[str]) -> ExposureLimits:
    """Read `live/exposure.json`. Missing fields take the defaults; bad fields refuse."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("exposure limits must be a JSON object")

    max_per_underlying = int(payload.get("max_per_underlying", 1))
    max_per_expiry = int(payload.get("max_per_expiry", 2))
    if max_per_underlying < 1:
        raise ValueError("max_per_underlying must be >= 1")
    if max_per_expiry < 1:
        raise ValueError("max_per_expiry must be >= 1")

    cap = payload.get("max_premium_per_underlying_usd")
    premium_cap = float(cap) if cap is not None else None
    if premium_cap is not None and premium_cap <= 0:
        raise ValueError("max_premium_per_underlying_usd must be > 0 when set")

    return ExposureLimits(
        max_per_underlying=max_per_underlying,
        max_per_expiry=max_per_expiry,
        max_premium_per_underlying_usd=premium_cap,
    )

"""Wire-format types for the MFF hot path. Data only - no behaviour, no I/O."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class Side(StrEnum):
    CALL = "CALL"
    PUT = "PUT"

    @property
    def is_call(self) -> bool:
        return self is Side.CALL


class AlertKind(StrEnum):
    """What a Discord post IS. The engine only ever acts on these."""

    ENTRY = "entry"          # a new position: side + ticker + strike
    ADD = "add"              # "adding at 1.30", "new avg 1.39"
    TRIM = "trim"            # "trim", "runners at $3.00", "%"-scaled exit
    CLOSE = "close"          # "close", "closed fully", "exit"
    STOP = "stop"            # "set SL", "stand down", "STOP"
    IGNORE = "ignore"        # not actionable (chatter, lotto, excluded channel)


class IgnoreReason(StrEnum):
    EXCLUDED_CHANNEL = "excluded_channel"
    TICKER_EXCLUDED = "ticker_excluded"
    LOTTO_FLAG = "lotto_flag"
    NO_SIGNAL = "no_signal"
    CHATTER = "chatter"


@dataclass(frozen=True)
class RawMessage:
    """One Discord MESSAGE_CREATE, already through the Gateway."""

    channel: str                 # human channel name, e.g. "etf-options"
    channel_id: str
    text: str
    message_id: str = ""
    ts: float = field(default_factory=time.time)   # epoch seconds, receive time


@dataclass(frozen=True)
class Signal:
    """A parsed, classified post. `kind` decides what the engine does with it."""

    kind: AlertKind
    channel: str = ""
    channel_id: str = ""
    ticker: str = ""
    side: Side | None = None
    strike: float | None = None
    expiry: str = ""                  # "0DTE" | "2026-09-11" | "Dec 5"
    his_bid: float | None = None      # the price he posted
    pct: float | None = None          # "%" exit target if stated
    price: float | None = None        # a bare price in an add/close/runner post
    tags: tuple[str, ...] = ()        # PLATINUM / DIAMOND ...
    flags: tuple[str, ...] = ()       # LOTTO / ODTE / ER / FOMC / RISKY
    ignore_reason: IgnoreReason | None = None
    raw: str = ""

    @property
    def contract_key(self) -> str:
        """Identity of the option this signal is about, for the position book."""
        if not (self.ticker and self.side and self.strike is not None):
            return ""
        return f"{self.ticker}|{self.side.value}|{self.strike:g}|{self.expiry or '?'}"

    @property
    def actionable(self) -> bool:
        return self.kind is not AlertKind.IGNORE


@dataclass(frozen=True)
class Quote:
    """Live option quote for the contract under consideration."""

    bid: float
    ask: float
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class OrderIntent:
    """What we want the broker to do. `reason` is always populated for the audit."""

    action: str                  # "buy" | "sell"
    qty: int
    limit: float
    contract_key: str
    reason: str
    idempotency_key: str = ""


@dataclass
class Position:
    contract_key: str
    ticker: str
    side: Side
    contracts: int
    avg_price: float
    opened_ts: float
    channel: str = ""
    adds: int = 0
    trimmed: int = 0
    breakeven_stop: float | None = None

    @property
    def is_open(self) -> bool:
        return self.contracts > 0


@dataclass(frozen=True)
class Fill:
    """One structured row in the append-only fill log."""

    seq: int
    ts: float
    event: str                   # "buy" | "sell" | "skip" | "reject" | "halt"
    contract_key: str
    qty: int
    price: float
    reason: str
    accepted: bool = True
    latency_ms: int | None = None
    note: str = ""

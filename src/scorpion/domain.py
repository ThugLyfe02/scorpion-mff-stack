from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal


class EventKind(StrEnum):
    ENTRY = "ENTRY"
    ADD = "ADD"
    TRIM = "TRIM"
    EXIT = "EXIT"
    STOP = "STOP"
    IGNORE = "IGNORE"
    AMBIGUOUS = "AMBIGUOUS"


class PositionStatus(StrEnum):
    FLAT = "FLAT"
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    HALTED = "HALTED"


class EffectKind(StrEnum):
    PROPOSE_OPEN = "PROPOSE_OPEN"
    PROPOSE_ADD = "PROPOSE_ADD"
    PROPOSE_TRIM = "PROPOSE_TRIM"
    PROPOSE_CLOSE = "PROPOSE_CLOSE"
    HALT = "HALT"
    REVIEW = "REVIEW"


@dataclass(frozen=True, slots=True)
class RawDiscordMessage:
    message_id: str
    guild_id: str
    channel_id: str
    author_id: str
    content: str
    source_ts_utc: datetime
    received_ts_utc: datetime
    edited_ts_utc: datetime | None = None
    referenced_message_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("source_ts_utc", "received_ts_utc"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.source_ts_utc.tzinfo != UTC:
            object.__setattr__(self, "source_ts_utc", self.source_ts_utc.astimezone(UTC))
        if self.received_ts_utc.tzinfo != UTC:
            object.__setattr__(self, "received_ts_utc", self.received_ts_utc.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class SignalEvent:
    event_id: str
    message_id: str
    kind: EventKind
    channel_id: str
    author_id: str
    source_ts_utc: datetime
    received_ts_utc: datetime
    ticker: str | None = None
    option_side: Literal["CALL", "PUT"] | None = None
    strike: Decimal | None = None
    expiry: date | None = None
    referenced_price: Decimal | None = None
    referenced_pct: Decimal | None = None
    raw_text: str = ""
    parser_version: str = "v1"
    reason: str = ""

    @property
    def contract_key(self) -> str | None:
        if not all((self.ticker, self.option_side, self.strike is not None, self.expiry)):
            return None
        return f"{self.ticker}|{self.option_side}|{self.strike}|{self.expiry.isoformat()}"


@dataclass(frozen=True, slots=True)
class PositionState:
    contract_key: str
    status: PositionStatus = PositionStatus.FLAT
    generation: int = 0
    source_entry_message_id: str | None = None
    last_source_ts_utc: datetime | None = None
    quantity: int = 0
    average_price: Decimal = Decimal("0")
    added_once: bool = False
    last_reason: str = ""


@dataclass(frozen=True, slots=True)
class Effect:
    kind: EffectKind
    contract_key: str | None
    source_event_id: str
    generation: int
    reason: str
    quantity_hint: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BookState:
    positions: dict[str, PositionState] = field(default_factory=dict)
    halted: bool = False
    seen_event_ids: frozenset[str] = frozenset()
    first_entry_proposed_on: date | None = None

    def with_position(self, position: PositionState) -> BookState:
        positions = dict(self.positions)
        positions[position.contract_key] = position
        return replace(self, positions=positions)

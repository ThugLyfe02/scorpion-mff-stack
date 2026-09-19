from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

from .domain import EventKind, SignalEvent


@dataclass(frozen=True, slots=True)
class ContractDescriptor:
    ticker: str
    option_side: Literal["CALL", "PUT"]
    strike: Decimal
    expiry: date

    @property
    def contract_key(self) -> str:
        return f"{self.ticker}|{self.option_side}|{self.strike}|{self.expiry.isoformat()}"


class ContractCatalog(Protocol):
    def contains(self, contract_key: str) -> bool: ...

    def nearby(self, event: SignalEvent) -> Sequence[ContractDescriptor]: ...


class InMemoryContractCatalog:
    def __init__(self, contracts: Sequence[ContractDescriptor]) -> None:
        self._contracts = tuple(contracts)
        self._keys = frozenset(contract.contract_key for contract in contracts)

    def contains(self, contract_key: str) -> bool:
        return contract_key in self._keys

    def nearby(self, event: SignalEvent) -> Sequence[ContractDescriptor]:
        if event.ticker is None or event.option_side is None or event.expiry is None:
            return ()
        return tuple(
            contract
            for contract in self._contracts
            if contract.ticker == event.ticker
            and contract.option_side == event.option_side
            and contract.expiry == event.expiry
        )


class ContractValidationStatus(StrEnum):
    EXACT = "EXACT"
    REVIEW = "REVIEW"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class ContractValidation:
    status: ContractValidationStatus
    reason: str
    candidate_keys: tuple[str, ...] = ()


def validate_contract(event: SignalEvent, catalog: ContractCatalog) -> ContractValidation:
    if event.kind is not EventKind.ENTRY:
        return ContractValidation(ContractValidationStatus.NOT_APPLICABLE, "not_entry")
    key = event.contract_key
    if key is None:
        return ContractValidation(ContractValidationStatus.REVIEW, "incomplete_contract")
    if catalog.contains(key):
        return ContractValidation(ContractValidationStatus.EXACT, "exact_catalog_match")
    candidates = tuple(contract.contract_key for contract in catalog.nearby(event))
    return ContractValidation(
        ContractValidationStatus.REVIEW,
        "contract_not_in_catalog",
        candidate_keys=candidates[:5],
    )


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    bid: Decimal
    ask: Decimal
    observed_ts_utc: datetime

    def __post_init__(self) -> None:
        if self.observed_ts_utc.tzinfo is None or self.observed_ts_utc.utcoffset() is None:
            raise ValueError("quote timestamp must be timezone-aware")


class QuoteQualityStatus(StrEnum):
    GOOD = "GOOD"
    REVIEW = "REVIEW"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class QuoteValidation:
    status: QuoteQualityStatus
    flags: tuple[str, ...]
    age_ms: float
    spread_ratio: Decimal | None
    reference_deviation: Decimal | None


def validate_quote(
    reference_price: Decimal,
    quote: QuoteSnapshot,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(seconds=3),
    max_spread_ratio: Decimal = Decimal("0.35"),
    max_reference_deviation: Decimal = Decimal("0.25"),
) -> QuoteValidation:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    age = now - quote.observed_ts_utc.astimezone(UTC)
    age_ms = age.total_seconds() * 1_000.0
    flags: list[str] = []
    if quote.bid < 0 or quote.ask < 0 or quote.ask < quote.bid:
        return QuoteValidation(QuoteQualityStatus.INVALID, ("invalid_market",), age_ms, None, None)
    if age < timedelta(0) or age > max_age:
        flags.append("stale_quote")

    midpoint = (quote.bid + quote.ask) / Decimal("2")
    spread_ratio = (quote.ask - quote.bid) / midpoint if midpoint > 0 else None
    if spread_ratio is not None and spread_ratio > max_spread_ratio:
        flags.append("wide_spread")

    reference_deviation: Decimal | None = None
    if reference_price > 0:
        reference_deviation = (quote.ask - reference_price) / reference_price
        if reference_deviation > max_reference_deviation:
            flags.append("ask_above_reference_threshold")
    else:
        flags.append("invalid_reference_price")

    status = QuoteQualityStatus.REVIEW if flags else QuoteQualityStatus.GOOD
    return QuoteValidation(status, tuple(flags), age_ms, spread_ratio, reference_deviation)

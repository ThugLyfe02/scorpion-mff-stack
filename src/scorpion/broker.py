from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from .domain import Effect, EffectKind


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    REVIEW_ONLY = "REVIEW_ONLY"


@dataclass(frozen=True, slots=True)
class Quote:
    bid: Decimal
    ask: Decimal
    observed_ts_utc: datetime


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    effect: Effect
    mode: ExecutionMode
    status: str
    quantity: int
    fill_price: Decimal | None
    submitted_ts_utc: datetime
    note: str = ""


class QuoteProvider(Protocol):
    def quote(self, contract_key: str) -> Quote: ...


class Broker(Protocol):
    def execute(
        self, effect: Effect, quantity: int, limit_price: Decimal | None
    ) -> ExecutionResult: ...


class PaperBroker:
    """Deterministic paper broker used for shadow runs and replay.

    It never talks to a brokerage. Production live submission is intentionally not implemented
    in this package; a separate approved integration can consume reviewed effects.
    """

    def execute(
        self, effect: Effect, quantity: int, limit_price: Decimal | None
    ) -> ExecutionResult:
        if effect.kind not in {
            EffectKind.PROPOSE_OPEN,
            EffectKind.PROPOSE_ADD,
            EffectKind.PROPOSE_TRIM,
            EffectKind.PROPOSE_CLOSE,
        }:
            return ExecutionResult(
                effect=effect,
                mode=ExecutionMode.PAPER,
                status="NOOP",
                quantity=0,
                fill_price=None,
                submitted_ts_utc=datetime.now(UTC),
            )
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        return ExecutionResult(
            effect=effect,
            mode=ExecutionMode.PAPER,
            status="PAPER_FILLED",
            quantity=quantity,
            fill_price=limit_price,
            submitted_ts_utc=datetime.now(UTC),
            note="shadow execution only",
        )


class ReviewOnlyBroker:
    def execute(
        self, effect: Effect, quantity: int, limit_price: Decimal | None
    ) -> ExecutionResult:
        return ExecutionResult(
            effect=effect,
            mode=ExecutionMode.REVIEW_ONLY,
            status="AWAITING_HUMAN_AUTHORIZATION",
            quantity=quantity,
            fill_price=None,
            submitted_ts_utc=datetime.now(UTC),
            note="No live brokerage submission is implemented by this adapter.",
        )

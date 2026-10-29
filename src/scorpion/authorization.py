from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from .broker import Quote
from .config import Policy
from .domain import BookState, Effect, EffectKind, PositionState, PositionStatus
from .pricing import dislocation, entry_limit
from .sizing import DEFAULT_CLIPS, ClipTable, SizingError, size_effect

ORDER_KINDS = frozenset(
    {
        EffectKind.PROPOSE_OPEN,
        EffectKind.PROPOSE_ADD,
        EffectKind.PROPOSE_TRIM,
        EffectKind.PROPOSE_CLOSE,
    }
)
BUY_KINDS = frozenset({EffectKind.PROPOSE_OPEN, EffectKind.PROPOSE_ADD})
NON_LIVE_STATUSES = frozenset(
    {PositionStatus.CLOSED, PositionStatus.FLAT, PositionStatus.HALTED}
)


@dataclass(frozen=True, slots=True)
class ApprovalEnvelope:
    effect: Effect
    approved_by: str
    approved_ts_utc: datetime
    observed_quote_ts_utc: datetime


def validate_approval(
    envelope: ApprovalEnvelope,
    position: PositionState,
    *,
    max_quote_age: timedelta = timedelta(seconds=3),
    now: datetime | None = None,
) -> None:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if not envelope.approved_by.strip():
        raise ValueError("approver identity required")
    if envelope.effect.generation != position.generation:
        raise ValueError("stale generation")
    if envelope.approved_ts_utc > now:
        raise ValueError("approval timestamp is in the future")
    quote_age = now - envelope.observed_quote_ts_utc.astimezone(UTC)
    if quote_age < timedelta(0) or quote_age > max_quote_age:
        raise ValueError("quote is stale")


# ── the executable ladder ────────────────────────────────────────────────────
# `validate_approval` answers "may a human approve this?". `authorize` answers the
# narrower, harder question: "may an ORDER leave the box right now, at this price, in
# this size?". It returns a named refusal instead of raising, because a refusal is a
# normal outcome that the log has to explain - a crash is not.


@dataclass(frozen=True, slots=True)
class OrderEnvelope:
    """Everything a broker adapter needs, and nothing it does not."""

    contract_key: str
    side: Literal["BUY", "SELL"]
    quantity: int
    limit_price: Decimal
    generation: int
    idempotency_key: str
    effect_kind: EffectKind

    def to_payload(self) -> dict[str, str]:
        """A flat, string-only representation for the durable outbox."""
        return {
            "contract_key": self.contract_key,
            "side": self.side,
            "quantity": str(self.quantity),
            "limit_price": str(self.limit_price),
            "generation": str(self.generation),
            "idempotency_key": self.idempotency_key,
            "effect_kind": self.effect_kind.value,
        }


@dataclass(frozen=True, slots=True)
class Authorization:
    ok: bool
    reason: str
    envelope: OrderEnvelope | None = None


def _refuse(reason: str) -> Authorization:
    return Authorization(ok=False, reason=reason)


def idempotency_key(effect: Effect) -> str:
    """Stable for one proposal, distinct across generations and kinds.

    Replaying the same event produces the same key, so a durable dispatcher can retry a
    submission without risking a double fill; a new generation produces a new key, so a
    genuine re-entry is not suppressed as a duplicate.
    """
    return (
        f"{effect.source_event_id}:{effect.contract_key}:"
        f"{effect.generation}:{effect.kind.value}"
    )


def authorize(
    effect: Effect,
    *,
    state: BookState,
    quote: Quote | None,
    policy: Policy,
    now_utc: datetime,
    reference_price: Decimal | None = None,
    live_enabled: bool | None = None,
    channel_id: str = "",
    clips: ClipTable = DEFAULT_CLIPS,
) -> Authorization:
    """Decide whether this effect may become an order right now."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")

    if effect.kind not in ORDER_KINDS:
        return _refuse("not_an_order_effect")
    if effect.contract_key is None:
        return _refuse("effect_without_contract")
    if live_enabled is None:
        live_enabled = policy.live_submission_enabled
    if not live_enabled:
        return _refuse("live_submission_disabled")
    if state.halted:
        return _refuse("system_halted")

    position = state.positions.get(effect.contract_key)
    if position is None:
        return _refuse("unknown_position")
    if effect.generation != position.generation:
        return _refuse(
            f"stale_generation:effect={effect.generation},book={position.generation}"
        )
    if position.status in NON_LIVE_STATUSES:
        return _refuse(f"position_not_live:{position.status.value}")

    if quote is None:
        return _refuse("no_quote")
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return _refuse("invalid_quote")

    age = now_utc - quote.observed_ts_utc
    if age < timedelta(0):
        return _refuse("quote_from_future")
    if age > timedelta(seconds=policy.quote_max_age_seconds):
        return _refuse(f"stale_quote:{int(age.total_seconds())}s")

    is_buy = effect.kind in BUY_KINDS
    if is_buy:
        if reference_price is None:
            limit_price = quote.ask
        else:
            if reference_price <= 0:
                return _refuse("invalid_reference_price")
            if dislocation(reference_price, quote.ask) > policy.max_dislocation_from_reference:
                off = dislocation(reference_price, quote.ask) * 100
                cap = policy.max_dislocation_from_reference * 100
                return _refuse(f"price_dislocation:{off:.1f}%>{cap:.1f}%")
            limit_price = entry_limit(reference_price, quote.ask, policy)
    else:
        limit_price = quote.bid

    if limit_price <= 0:
        return _refuse("invalid_limit_price")

    if is_buy:
        if effect.quantity_hint is not None:
            quantity = effect.quantity_hint
        else:
            # Size here, not in the reducer: this is the first point where a real limit
            # price exists, and a clip is a dollar budget divided by that price.
            try:
                quantity = size_effect(
                    quantity_hint=None,
                    limit_price=limit_price,
                    channel_id=channel_id,
                    is_add=effect.kind is EffectKind.PROPOSE_ADD,
                    clips=clips,
                )
            except SizingError as exc:
                return _refuse(f"sizing_failed:{exc}")
    else:
        held = position.quantity
        quantity = min(effect.quantity_hint or held, held)
    if quantity <= 0:
        return _refuse("zero_quantity")

    return Authorization(
        ok=True,
        reason="authorized",
        envelope=OrderEnvelope(
            contract_key=effect.contract_key,
            side="BUY" if is_buy else "SELL",
            quantity=quantity,
            limit_price=limit_price,
            generation=effect.generation,
            idempotency_key=idempotency_key(effect),
            effect_kind=effect.kind,
        ),
    )

from __future__ import annotations

from dataclasses import replace

from .domain import BookState, EventKind, SignalEvent


def associate_followup(event: SignalEvent, state: BookState, referenced_contract_key: str | None = None) -> SignalEvent:
    """Attach a follow-up to exactly one live contract.

    Priority:
    1. explicit Discord reply/reference resolution supplied by caller;
    2. a unique currently-live contract in the same book;
    3. otherwise leave unassociated and force review.

    This deliberately refuses heuristic ticker guessing for money-path events.
    """
    if event.kind not in {EventKind.ADD, EventKind.TRIM, EventKind.EXIT}:
        return event
    if event.contract_key is not None:
        return event

    if referenced_contract_key and referenced_contract_key in state.positions:
        p = state.positions[referenced_contract_key]
        ticker, side, strike, expiry = referenced_contract_key.split("|", 3)
        return replace(
            event,
            ticker=ticker,
            option_side=side,  # type: ignore[arg-type]
            strike=__import__("decimal").Decimal(strike),
            expiry=__import__("datetime").date.fromisoformat(expiry),
            reason=f"{event.reason}:associated_by_reply",
        )

    live = [
        p for p in state.positions.values()
        if p.status.value in {"PENDING_ENTRY", "OPEN", "CLOSING"}
    ]
    if len(live) == 1:
        ticker, side, strike, expiry = live[0].contract_key.split("|", 3)
        return replace(
            event,
            ticker=ticker,
            option_side=side,  # type: ignore[arg-type]
            strike=__import__("decimal").Decimal(strike),
            expiry=__import__("datetime").date.fromisoformat(expiry),
            reason=f"{event.reason}:associated_unique_live",
        )
    return event

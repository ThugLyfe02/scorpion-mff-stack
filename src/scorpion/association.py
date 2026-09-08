from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Literal

from .accuracy import AssociationEvidence
from .domain import BookState, EventKind, PositionState, PositionStatus, SignalEvent

_TICKER_TOKEN_RE = re.compile(r"\b[A-Z]{1,6}\b")
_CALL_RE = re.compile(r"\b(?:CALL|CALLS|C)\b", re.IGNORECASE)
_PUT_RE = re.compile(r"\b(?:PUT|PUTS|P)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AssociationDecision:
    event: SignalEvent
    evidence: AssociationEvidence


def _live_positions(state: BookState) -> list[PositionState]:
    return [
        position
        for position in state.positions.values()
        if position.status
        in {PositionStatus.PENDING_ENTRY, PositionStatus.OPEN, PositionStatus.CLOSING}
    ]


def _parts(
    contract_key: str,
) -> tuple[str, Literal["CALL", "PUT"], Decimal, date]:
    ticker, side_raw, strike, expiry = contract_key.split("|", 3)
    side: Literal["CALL", "PUT"] = "CALL" if side_raw == "CALL" else "PUT"
    return ticker, side, Decimal(strike), date.fromisoformat(expiry)


def _attach(event: SignalEvent, position: PositionState, reason_suffix: str) -> SignalEvent:
    ticker, side, strike, expiry = _parts(position.contract_key)
    return replace(
        event,
        ticker=ticker,
        option_side=side,
        strike=strike,
        expiry=expiry,
        reason=f"{event.reason}:{reason_suffix}",
    )


def _explicit_ticker_candidates(
    event: SignalEvent,
    candidates: list[PositionState],
) -> list[PositionState]:
    tokens = set(_TICKER_TOKEN_RE.findall(event.raw_text.upper()))
    if not tokens:
        return []
    ticker_matches = [
        position for position in candidates if _parts(position.contract_key)[0] in tokens
    ]
    if not ticker_matches:
        return []

    has_call = bool(_CALL_RE.search(event.raw_text))
    has_put = bool(_PUT_RE.search(event.raw_text))
    if has_call ^ has_put:
        side = "CALL" if has_call else "PUT"
        sided = [
            position
            for position in ticker_matches
            if _parts(position.contract_key)[1] == side
        ]
        if sided:
            return sided
    return ticker_matches


def associate_followup_with_evidence(
    event: SignalEvent,
    state: BookState,
    referenced_contract_key: str | None = None,
) -> AssociationDecision:
    """Attach a follow-up only when deterministic evidence identifies one contract.

    Evidence ladder:
    1. explicit Discord reply/reference;
    2. event already carries an exact contract;
    3. explicit ticker (+ optional side) uniquely identifies a live contract;
    4. unique live position from the same author+channel lineage;
    5. unique live position overall;
    6. otherwise refuse association and force review.
    """
    if event.kind not in {EventKind.ADD, EventKind.TRIM, EventKind.EXIT}:
        return AssociationDecision(event, AssociationEvidence("not_followup", 1.0, 0))
    if event.contract_key is not None:
        return AssociationDecision(event, AssociationEvidence("exact_contract", 1.0, 1))

    if referenced_contract_key and referenced_contract_key in state.positions:
        position = state.positions[referenced_contract_key]
        attached = _attach(event, position, "associated_by_reply")
        return AssociationDecision(attached, AssociationEvidence("reply_reference", 1.0, 1))

    live = _live_positions(state)
    ticker_candidates = _explicit_ticker_candidates(event, live)
    if len(ticker_candidates) == 1:
        attached = _attach(event, ticker_candidates[0], "associated_by_explicit_ticker")
        return AssociationDecision(
            attached,
            AssociationEvidence("explicit_ticker", 0.97, 1),
        )
    if len(ticker_candidates) > 1:
        return AssociationDecision(
            event,
            AssociationEvidence("explicit_ticker_ambiguous", 0.0, len(ticker_candidates)),
        )

    lineage = [
        position
        for position in live
        if position.source_channel_id == event.channel_id
        and position.source_author_id == event.author_id
    ]
    if len(lineage) == 1:
        attached = _attach(event, lineage[0], "associated_by_source_lineage")
        return AssociationDecision(
            attached,
            AssociationEvidence("source_lineage", 0.93, 1),
        )
    if len(lineage) > 1:
        return AssociationDecision(
            event,
            AssociationEvidence("source_lineage_ambiguous", 0.0, len(lineage)),
        )

    if len(live) == 1:
        attached = _attach(event, live[0], "associated_unique_live")
        return AssociationDecision(attached, AssociationEvidence("unique_live", 0.85, 1))

    return AssociationDecision(event, AssociationEvidence("unassociated", 0.0, len(live)))


def associate_followup(
    event: SignalEvent,
    state: BookState,
    referenced_contract_key: str | None = None,
) -> SignalEvent:
    return associate_followup_with_evidence(event, state, referenced_contract_key).event

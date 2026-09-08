from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from .domain import EventKind, RawDiscordMessage, SignalEvent
from .parser import parse_message

ParserFn = Callable[[RawDiscordMessage, frozenset[str] | None], SignalEvent]


@dataclass(frozen=True, slots=True)
class GrammarCase:
    name: str
    text: str
    expected_kind: EventKind
    expected_contract_key: str | None = None


@dataclass(frozen=True, slots=True)
class GrammarFailure:
    name: str
    expected_kind: EventKind
    actual_kind: EventKind
    expected_contract_key: str | None
    actual_contract_key: str | None


@dataclass(frozen=True, slots=True)
class GrammarReport:
    total: int
    passed: int
    failures: tuple[GrammarFailure, ...]


def option_entry_grammar(
    *,
    ticker: str,
    strike: Decimal,
    side: str,
    expiry_text: str,
    price: Decimal,
    expected_contract_key: str,
) -> tuple[GrammarCase, ...]:
    normalized_side = side.upper()
    if normalized_side not in {"CALL", "PUT"}:
        raise ValueError("side must be CALL or PUT")
    cp = "C" if normalized_side == "CALL" else "P"
    canonical = f"{ticker} {strike}{cp} {expiry_text} @ {price}"
    return (
        GrammarCase("entry_at", canonical, EventKind.ENTRY, expected_contract_key),
        GrammarCase(
            "entry_bid",
            f"{ticker} {strike}{cp} {expiry_text} bid {price}",
            EventKind.ENTRY,
            expected_contract_key,
        ),
        GrammarCase(
            "entry_premium",
            f"{ticker} {strike}{normalized_side} {expiry_text} premium {price}",
            EventKind.ENTRY,
            expected_contract_key,
        ),
        GrammarCase(
            "entry_side_first",
            f"{normalized_side} {ticker} {strike} {expiry_text} at {price}",
            EventKind.ENTRY,
            expected_contract_key,
        ),
        GrammarCase("negated_buy", f"Do not buy {canonical}", EventKind.AMBIGUOUS),
        GrammarCase("negated_enter", f"Never enter {canonical}", EventKind.AMBIGUOUS),
        GrammarCase(
            "historical_recap",
            f"Recap from yesterday: {canonical}",
            EventKind.AMBIGUOUS,
        ),
    )


def evaluate_grammar(
    base: RawDiscordMessage,
    cases: Sequence[GrammarCase],
    *,
    parser: ParserFn = parse_message,
    allowed_author_ids: frozenset[str] | None = None,
) -> GrammarReport:
    failures: list[GrammarFailure] = []
    for index, case in enumerate(cases):
        raw = replace(base, message_id=f"{base.message_id}-grammar-{index}", content=case.text)
        event = parser(raw, allowed_author_ids)
        contract_matches = (
            case.expected_contract_key is None
            or event.contract_key == case.expected_contract_key
        )
        if event.kind is not case.expected_kind or not contract_matches:
            failures.append(
                GrammarFailure(
                    name=case.name,
                    expected_kind=case.expected_kind,
                    actual_kind=event.kind,
                    expected_contract_key=case.expected_contract_key,
                    actual_contract_key=event.contract_key,
                )
            )
    return GrammarReport(len(cases), len(cases) - len(failures), tuple(failures))

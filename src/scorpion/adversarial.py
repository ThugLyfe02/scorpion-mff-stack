from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from .accuracy import ACTIONABLE_KINDS
from .domain import RawDiscordMessage, SignalEvent

Parser = Callable[[RawDiscordMessage], SignalEvent]

_ZERO_WIDTH = "\u200b"


@dataclass(frozen=True, slots=True)
class MutationResult:
    text: str
    kind_changed: bool
    contract_changed: bool
    predicted_kind: str
    predicted_contract: str | None


@dataclass(frozen=True, slots=True)
class RobustnessReport:
    total: int
    invariant: int
    kind_changes: int
    contract_changes: int
    results: tuple[MutationResult, ...]


@dataclass(frozen=True, slots=True)
class GrammarMutation:
    label: str
    text: str
    should_block_action: bool = True


@dataclass(frozen=True, slots=True)
class GrammarMutationResult:
    label: str
    text: str
    predicted_kind: str
    action_leak: bool


@dataclass(frozen=True, slots=True)
class GrammarRobustnessReport:
    total: int
    action_leaks: int
    results: tuple[GrammarMutationResult, ...]


def surface_mutations(text: str) -> tuple[str, ...]:
    tokens = text.split()
    candidates = {
        text,
        "  ".join(tokens),
        "\t".join(tokens),
        "\n".join(tokens),
        f"  {text}  ",
        text.lower(),
        text.upper(),
        _ZERO_WIDTH.join(text),
        text.replace("@", " @ "),
        text.replace("%", " % "),
    }
    return tuple(sorted(candidates))


def grammar_mutations(text: str) -> tuple[GrammarMutation, ...]:
    return (
        GrammarMutation("negated", f"Do not {text}"),
        GrammarMutation("conditional_if", f"If confirmed, {text}"),
        GrammarMutation("conditional_maybe", f"Maybe {text}"),
        GrammarMutation("historical_prefix", f"Yesterday {text}"),
        GrammarMutation("historical_suffix", f"{text} — recap"),
    )


def evaluate_surface_robustness(
    raw: RawDiscordMessage,
    parser: Parser,
) -> RobustnessReport:
    baseline = parser(raw)
    results: list[MutationResult] = []
    for index, text in enumerate(surface_mutations(raw.content)):
        mutated = replace(raw, message_id=f"{raw.message_id}:mutation:{index}", content=text)
        predicted = parser(mutated)
        results.append(
            MutationResult(
                text=text,
                kind_changed=predicted.kind is not baseline.kind,
                contract_changed=predicted.contract_key != baseline.contract_key,
                predicted_kind=predicted.kind.value,
                predicted_contract=predicted.contract_key,
            )
        )
    kind_changes = sum(result.kind_changed for result in results)
    contract_changes = sum(result.contract_changed for result in results)
    return RobustnessReport(
        total=len(results),
        invariant=len(results) - sum(
            result.kind_changed or result.contract_changed for result in results
        ),
        kind_changes=kind_changes,
        contract_changes=contract_changes,
        results=tuple(results),
    )


def evaluate_grammar_robustness(
    raw: RawDiscordMessage,
    parser: Parser,
) -> GrammarRobustnessReport:
    results: list[GrammarMutationResult] = []
    for index, mutation in enumerate(grammar_mutations(raw.content)):
        mutated = replace(
            raw,
            message_id=f"{raw.message_id}:grammar:{index}",
            content=mutation.text,
        )
        predicted = parser(mutated)
        leak = mutation.should_block_action and predicted.kind in ACTIONABLE_KINDS
        results.append(
            GrammarMutationResult(
                label=mutation.label,
                text=mutation.text,
                predicted_kind=predicted.kind.value,
                action_leak=leak,
            )
        )
    return GrammarRobustnessReport(
        total=len(results),
        action_leaks=sum(result.action_leak for result in results),
        results=tuple(results),
    )

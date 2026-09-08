from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .accuracy import ACTIONABLE_KINDS, percentile
from .adversarial import evaluate_grammar_robustness, evaluate_surface_robustness
from .counterfactual import (
    CounterfactualDelta,
    EvidenceParser,
    compare_counterfactual_runs,
    run_counterfactual,
)
from .domain import EventKind, RawDiscordMessage, SignalEvent

EventParser = Callable[[RawDiscordMessage], SignalEvent]


@dataclass(frozen=True, slots=True)
class TournamentCandidate:
    name: str
    parser: EvidenceParser


@dataclass(frozen=True, slots=True)
class CandidateScore:
    name: str
    labeled_samples: int
    accuracy: float
    actionable_precision: float
    ambiguity_rate: float
    parser_p95_us: float
    surface_kind_changes: int
    surface_contract_changes: int
    grammar_action_leaks: int
    review_effects: int
    action_escalations_vs_baseline: int
    downstream_delta: CounterfactualDelta | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class TournamentReport:
    baseline_name: str
    scores: tuple[CandidateScore, ...]


def _safe_div(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _event_parser(
    parser: EvidenceParser,
    allowed_author_ids: frozenset[str] | None,
) -> EventParser:
    def parse(item: RawDiscordMessage) -> SignalEvent:
        return parser(item, allowed_author_ids).event

    return parse


def run_parser_tournament(
    messages: Sequence[RawDiscordMessage],
    candidates: Sequence[TournamentCandidate],
    *,
    expected_kinds: Mapping[str, EventKind] | None = None,
    allowed_author_ids: frozenset[str] | None = None,
) -> TournamentReport:
    if not candidates:
        raise ValueError("at least one candidate is required")
    expected_kinds = expected_kinds or {}
    baseline_run = run_counterfactual(
        messages,
        parser=candidates[0].parser,
        allowed_author_ids=allowed_author_ids,
    )
    scores: list[CandidateScore] = []
    baseline_events = baseline_run.events

    for index, candidate in enumerate(candidates):
        candidate_parser = candidate.parser
        event_parser = _event_parser(candidate_parser, allowed_author_ids)
        run = (
            baseline_run
            if index == 0
            else run_counterfactual(
                messages,
                parser=candidate_parser,
                allowed_author_ids=allowed_author_ids,
            )
        )
        latencies: list[int] = []
        labeled = 0
        correct = 0
        predicted_actionable = 0
        correct_actionable = 0
        surface_kind_changes = 0
        surface_contract_changes = 0
        grammar_action_leaks = 0

        for raw in messages:
            decision = candidate_parser(raw, allowed_author_ids)
            latencies.append(decision.evidence.latency_us)
            expected = expected_kinds.get(raw.revision_id)
            if expected is not None:
                labeled += 1
                correct += int(decision.event.kind is expected)
                if decision.event.kind in ACTIONABLE_KINDS:
                    predicted_actionable += 1
                    correct_actionable += int(decision.event.kind is expected)

            surface = evaluate_surface_robustness(raw, event_parser)
            surface_kind_changes += surface.kind_changes
            surface_contract_changes += surface.contract_changes
            grammar = evaluate_grammar_robustness(raw, event_parser)
            grammar_action_leaks += grammar.action_leaks

        action_escalations = sum(
            old.kind not in ACTIONABLE_KINDS and new.kind in ACTIONABLE_KINDS
            for old, new in zip(baseline_events, run.events, strict=False)
        )
        delta = None if index == 0 else compare_counterfactual_runs(baseline_run, run)
        scores.append(
            CandidateScore(
                name=candidate.name,
                labeled_samples=labeled,
                accuracy=_safe_div(correct, labeled),
                actionable_precision=_safe_div(correct_actionable, predicted_actionable),
                ambiguity_rate=_safe_div(
                    sum(event.kind is EventKind.AMBIGUOUS for event in run.events),
                    len(run.events),
                ),
                parser_p95_us=percentile(latencies, 0.95),
                surface_kind_changes=surface_kind_changes,
                surface_contract_changes=surface_contract_changes,
                grammar_action_leaks=grammar_action_leaks,
                review_effects=run.review_effects,
                action_escalations_vs_baseline=action_escalations,
                downstream_delta=delta,
                fingerprint=run.fingerprint,
            )
        )
    return TournamentReport(candidates[0].name, tuple(scores))

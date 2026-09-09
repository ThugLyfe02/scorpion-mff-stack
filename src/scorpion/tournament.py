from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .accuracy import ACTIONABLE_KINDS
from .adversarial import evaluate_grammar_robustness, evaluate_surface_robustness
from .counterfactual import (
    CounterfactualDelta,
    EvidenceParser,
    compare_counterfactual_runs,
    run_counterfactual,
)
from .domain import EventKind, RawDiscordMessage, SignalEvent
from .profiling import profile_messages

EventParser = Callable[[RawDiscordMessage], SignalEvent]


@dataclass(frozen=True, slots=True)
class TournamentCandidate:
    name: str
    parser: EvidenceParser


@dataclass(frozen=True, slots=True)
class TournamentCriteria:
    min_labeled_samples: int = 100
    min_accuracy: float = 0.95
    min_actionable_precision: float = 0.995
    min_label_coverage: float = 0.80
    min_actionable_contract_coverage: float = 1.0
    min_distinct_labeled_kinds: int = 2
    max_action_escalations: int = 0
    max_contract_label_errors: int = 0
    max_grammar_action_leaks: int = 0
    max_surface_contract_changes: int = 0
    max_parser_p95_us: float = 2_000.0
    max_total_p95_us: float = 20_000.0

    def __post_init__(self) -> None:
        if self.min_labeled_samples <= 0:
            raise ValueError("min_labeled_samples must be positive")
        if self.min_distinct_labeled_kinds <= 0:
            raise ValueError("min_distinct_labeled_kinds must be positive")
        for name in (
            "min_accuracy",
            "min_actionable_precision",
            "min_label_coverage",
            "min_actionable_contract_coverage",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0,1]")


@dataclass(frozen=True, slots=True)
class CandidateScore:
    name: str
    labeled_samples: int
    accuracy: float
    actionable_precision: float
    ambiguity_rate: float
    parser_p95_us: float
    total_p95_us: float
    surface_kind_changes: int
    surface_contract_changes: int
    grammar_action_leaks: int
    contract_label_errors: int
    review_effects: int
    action_escalations_vs_baseline: int
    downstream_delta: CounterfactualDelta | None
    fingerprint: str
    qualified: bool
    failures: tuple[str, ...]
    label_coverage: float = 0.0
    distinct_labeled_kinds: int = 0
    actionable_labeled_samples: int = 0
    actionable_contract_labels: int = 0
    actionable_contract_coverage: float = 1.0
    missing_actionable_contract_labels: int = 0


@dataclass(frozen=True, slots=True)
class TournamentReport:
    baseline_name: str
    scores: tuple[CandidateScore, ...]
    ranked: tuple[CandidateScore, ...]


def _safe_div(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _event_parser(
    parser: EvidenceParser,
    allowed_author_ids: frozenset[str] | None,
) -> EventParser:
    def parse(item: RawDiscordMessage) -> SignalEvent:
        return parser(item, allowed_author_ids).event

    return parse


def _qualification_failures(
    *,
    labeled_samples: int,
    label_coverage: float,
    distinct_labeled_kinds: int,
    accuracy: float,
    actionable_precision: float,
    actionable_contract_coverage: float,
    missing_actionable_contract_labels: int,
    action_escalations: int,
    contract_label_errors: int,
    grammar_action_leaks: int,
    surface_contract_changes: int,
    parser_p95_us: float,
    total_p95_us: float,
    criteria: TournamentCriteria,
) -> tuple[str, ...]:
    failures: list[str] = []
    if labeled_samples < criteria.min_labeled_samples:
        failures.append(f"samples:{labeled_samples}<{criteria.min_labeled_samples}")
    if label_coverage < criteria.min_label_coverage:
        failures.append(
            f"label_coverage:{label_coverage:.6f}<{criteria.min_label_coverage:.6f}"
        )
    if distinct_labeled_kinds < criteria.min_distinct_labeled_kinds:
        failures.append(
            "distinct_labeled_kinds:"
            f"{distinct_labeled_kinds}<{criteria.min_distinct_labeled_kinds}"
        )
    if accuracy < criteria.min_accuracy:
        failures.append(f"accuracy:{accuracy:.6f}<{criteria.min_accuracy:.6f}")
    if actionable_precision < criteria.min_actionable_precision:
        failures.append(
            "actionable_precision:"
            f"{actionable_precision:.6f}<{criteria.min_actionable_precision:.6f}"
        )
    if actionable_contract_coverage < criteria.min_actionable_contract_coverage:
        failures.append(
            "actionable_contract_coverage:"
            f"{actionable_contract_coverage:.6f}<"
            f"{criteria.min_actionable_contract_coverage:.6f}"
        )
    if missing_actionable_contract_labels:
        failures.append(
            f"missing_actionable_contract_labels:{missing_actionable_contract_labels}"
        )
    if action_escalations > criteria.max_action_escalations:
        failures.append(
            f"action_escalations:{action_escalations}>{criteria.max_action_escalations}"
        )
    if contract_label_errors > criteria.max_contract_label_errors:
        failures.append(
            "contract_label_errors:"
            f"{contract_label_errors}>{criteria.max_contract_label_errors}"
        )
    if grammar_action_leaks > criteria.max_grammar_action_leaks:
        failures.append(
            f"grammar_action_leaks:{grammar_action_leaks}>{criteria.max_grammar_action_leaks}"
        )
    if surface_contract_changes > criteria.max_surface_contract_changes:
        failures.append(
            "surface_contract_changes:"
            f"{surface_contract_changes}>{criteria.max_surface_contract_changes}"
        )
    if parser_p95_us > criteria.max_parser_p95_us:
        failures.append(
            f"parser_p95_us:{parser_p95_us:.3f}>{criteria.max_parser_p95_us:.3f}"
        )
    if total_p95_us > criteria.max_total_p95_us:
        failures.append(
            f"total_p95_us:{total_p95_us:.3f}>{criteria.max_total_p95_us:.3f}"
        )
    return tuple(failures)


def run_parser_tournament(
    messages: Sequence[RawDiscordMessage],
    candidates: Sequence[TournamentCandidate],
    *,
    expected_kinds: Mapping[str, EventKind] | None = None,
    expected_contracts: Mapping[str, str | None] | None = None,
    allowed_author_ids: frozenset[str] | None = None,
    criteria: TournamentCriteria | None = None,
) -> TournamentReport:
    """Score parser candidates only when the evaluation truth is sufficiently complete.

    Actionable kind truth without actionable contract truth is deliberately insufficient for
    qualification. Otherwise a parser can receive credit for detecting ENTRY/ADD/EXIT while
    inventing the instrument. The tournament also reports total label coverage and class breadth
    so a tiny or one-class convenience subset cannot masquerade as representative validation.
    """
    if not candidates:
        raise ValueError("at least one candidate is required")
    expected_kinds = expected_kinds or {}
    expected_contracts = expected_contracts or {}
    criteria = criteria or TournamentCriteria()

    baseline_run = run_counterfactual(
        messages,
        parser=candidates[0].parser,
        allowed_author_ids=allowed_author_ids,
    )
    baseline_events = baseline_run.events
    scores: list[CandidateScore] = []

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
        profile = profile_messages(
            messages,
            parser=candidate_parser,
            allowed_author_ids=allowed_author_ids,
        )
        labeled = 0
        correct = 0
        predicted_actionable = 0
        correct_actionable = 0
        actionable_labeled = 0
        actionable_contract_labels = 0
        contract_label_errors = 0
        surface_kind_changes = 0
        surface_contract_changes = 0
        grammar_action_leaks = 0
        labeled_kinds: set[EventKind] = set()

        for raw, event in zip(messages, run.events, strict=False):
            revision_id = raw.revision_id
            expected = expected_kinds.get(revision_id)
            if expected is not None:
                labeled += 1
                labeled_kinds.add(expected)
                correct += int(event.kind is expected)
                if event.kind in ACTIONABLE_KINDS:
                    predicted_actionable += 1
                    correct_actionable += int(event.kind is expected)
                if expected in ACTIONABLE_KINDS:
                    actionable_labeled += 1
                    expected_contract = expected_contracts.get(revision_id)
                    if expected_contract is not None and expected_contract.strip():
                        actionable_contract_labels += 1
                        contract_label_errors += int(event.contract_key != expected_contract)
            elif revision_id in expected_contracts:
                contract_label_errors += int(
                    event.contract_key != expected_contracts[revision_id]
                )

            # Explicit None remains meaningful for non-actionable examples: it means the parser
            # is expected not to produce a contract. For actionable examples, None is incomplete
            # supervision and is counted as missing above rather than silently treated as truth.
            if (
                expected is not None
                and expected not in ACTIONABLE_KINDS
                and revision_id in expected_contracts
            ):
                contract_label_errors += int(
                    event.contract_key != expected_contracts[revision_id]
                )

            surface = evaluate_surface_robustness(raw, event_parser)
            surface_kind_changes += surface.kind_changes
            surface_contract_changes += surface.contract_changes
            grammar = evaluate_grammar_robustness(raw, event_parser)
            grammar_action_leaks += grammar.action_leaks

        accuracy = _safe_div(correct, labeled)
        actionable_precision = _safe_div(correct_actionable, predicted_actionable)
        label_coverage = _safe_div(labeled, len(messages))
        actionable_contract_coverage = (
            actionable_contract_labels / actionable_labeled if actionable_labeled else 1.0
        )
        missing_actionable_contract_labels = (
            actionable_labeled - actionable_contract_labels
        )
        action_escalations = sum(
            old.kind not in ACTIONABLE_KINDS and new.kind in ACTIONABLE_KINDS
            for old, new in zip(baseline_events, run.events, strict=False)
        )
        delta = None if index == 0 else compare_counterfactual_runs(baseline_run, run)
        failures = _qualification_failures(
            labeled_samples=labeled,
            label_coverage=label_coverage,
            distinct_labeled_kinds=len(labeled_kinds),
            accuracy=accuracy,
            actionable_precision=actionable_precision,
            actionable_contract_coverage=actionable_contract_coverage,
            missing_actionable_contract_labels=missing_actionable_contract_labels,
            action_escalations=action_escalations,
            contract_label_errors=contract_label_errors,
            grammar_action_leaks=grammar_action_leaks,
            surface_contract_changes=surface_contract_changes,
            parser_p95_us=profile.parse.p95_us,
            total_p95_us=profile.total.p95_us,
            criteria=criteria,
        )
        scores.append(
            CandidateScore(
                name=candidate.name,
                labeled_samples=labeled,
                accuracy=accuracy,
                actionable_precision=actionable_precision,
                ambiguity_rate=_safe_div(
                    sum(event.kind is EventKind.AMBIGUOUS for event in run.events),
                    len(run.events),
                ),
                parser_p95_us=profile.parse.p95_us,
                total_p95_us=profile.total.p95_us,
                surface_kind_changes=surface_kind_changes,
                surface_contract_changes=surface_contract_changes,
                grammar_action_leaks=grammar_action_leaks,
                contract_label_errors=contract_label_errors,
                review_effects=run.review_effects,
                action_escalations_vs_baseline=action_escalations,
                downstream_delta=delta,
                fingerprint=run.fingerprint,
                qualified=not failures,
                failures=failures,
                label_coverage=label_coverage,
                distinct_labeled_kinds=len(labeled_kinds),
                actionable_labeled_samples=actionable_labeled,
                actionable_contract_labels=actionable_contract_labels,
                actionable_contract_coverage=actionable_contract_coverage,
                missing_actionable_contract_labels=missing_actionable_contract_labels,
            )
        )

    ranked = tuple(
        sorted(
            scores,
            key=lambda result: (
                not result.qualified,
                result.missing_actionable_contract_labels,
                result.action_escalations_vs_baseline,
                result.contract_label_errors,
                result.grammar_action_leaks,
                result.surface_contract_changes,
                -result.actionable_contract_coverage,
                -result.label_coverage,
                -result.actionable_precision,
                -result.accuracy,
                result.ambiguity_rate,
                result.review_effects,
                result.total_p95_us,
            ),
        )
    )
    return TournamentReport(candidates[0].name, tuple(scores), ranked)

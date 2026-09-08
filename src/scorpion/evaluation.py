from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .accuracy import ACTIONABLE_KINDS, AccuracyReport
from .domain import RawDiscordMessage, SignalEvent

ParserFn = Callable[[RawDiscordMessage], SignalEvent]


@dataclass(frozen=True, slots=True)
class ParserDiff:
    message_id: str
    baseline_kind: str
    candidate_kind: str
    baseline_contract: str | None
    candidate_contract: str | None
    action_escalation: bool
    contract_changed: bool


@dataclass(frozen=True, slots=True)
class VersionComparison:
    total: int
    changed: int
    action_escalations: int
    contract_changes: int
    diffs: tuple[ParserDiff, ...]


def compare_parser_versions(
    messages: Sequence[RawDiscordMessage],
    baseline: ParserFn,
    candidate: ParserFn,
) -> VersionComparison:
    diffs: list[ParserDiff] = []
    action_escalations = 0
    contract_changes = 0
    for raw in messages:
        old = baseline(raw)
        new = candidate(raw)
        changed = old.kind is not new.kind or old.contract_key != new.contract_key
        if not changed:
            continue
        escalation = old.kind not in ACTIONABLE_KINDS and new.kind in ACTIONABLE_KINDS
        contract_changed = old.contract_key != new.contract_key
        action_escalations += int(escalation)
        contract_changes += int(contract_changed)
        diffs.append(
            ParserDiff(
                message_id=raw.message_id,
                baseline_kind=old.kind.value,
                candidate_kind=new.kind.value,
                baseline_contract=old.contract_key,
                candidate_contract=new.contract_key,
                action_escalation=escalation,
                contract_changed=contract_changed,
            )
        )
    return VersionComparison(
        total=len(messages),
        changed=len(diffs),
        action_escalations=action_escalations,
        contract_changes=contract_changes,
        diffs=tuple(diffs),
    )


@dataclass(frozen=True, slots=True)
class PromotionCriteria:
    min_adjudicated_samples: int = 100
    min_accuracy: float = 0.95
    min_actionable_precision: float = 0.995
    max_ambiguity_rate: float = 0.35
    max_parser_p95_us: float = 2_000.0
    max_pipeline_p95_us: float = 20_000.0
    max_action_escalations_vs_baseline: int = 0


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promotable: bool
    failures: tuple[str, ...]


def evaluate_promotion(
    report: AccuracyReport,
    decision_health: dict[str, float | int],
    comparison: VersionComparison,
    criteria: PromotionCriteria | None = None,
) -> PromotionDecision:
    criteria = criteria or PromotionCriteria()
    failures: list[str] = []
    if report.total < criteria.min_adjudicated_samples:
        failures.append(f"samples:{report.total}<{criteria.min_adjudicated_samples}")
    if report.accuracy < criteria.min_accuracy:
        failures.append(f"accuracy:{report.accuracy:.6f}<{criteria.min_accuracy:.6f}")
    if report.actionable_precision < criteria.min_actionable_precision:
        failures.append(
            "actionable_precision:"
            f"{report.actionable_precision:.6f}<{criteria.min_actionable_precision:.6f}"
        )
    ambiguity = float(decision_health.get("ambiguity_rate", 0.0))
    if ambiguity > criteria.max_ambiguity_rate:
        failures.append(
            f"ambiguity_rate:{ambiguity:.6f}>{criteria.max_ambiguity_rate:.6f}"
        )
    parser_p95 = float(decision_health.get("parser_p95_us", 0.0))
    if parser_p95 > criteria.max_parser_p95_us:
        failures.append(
            f"parser_p95_us:{parser_p95:.3f}>{criteria.max_parser_p95_us:.3f}"
        )
    pipeline_p95 = float(decision_health.get("pipeline_p95_us", 0.0))
    if pipeline_p95 > criteria.max_pipeline_p95_us:
        failures.append(
            f"pipeline_p95_us:{pipeline_p95:.3f}>{criteria.max_pipeline_p95_us:.3f}"
        )
    if comparison.action_escalations > criteria.max_action_escalations_vs_baseline:
        failures.append(
            "action_escalations:"
            f"{comparison.action_escalations}>{criteria.max_action_escalations_vs_baseline}"
        )
    return PromotionDecision(promotable=not failures, failures=tuple(failures))

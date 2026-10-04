from dataclasses import replace

from scorpion.counterfactual import (
    CounterfactualRun,
    compare_counterfactual_runs,
    run_counterfactual,
)
from scorpion.domain import Effect, EffectKind, EventKind
from scorpion.parser import ParseDecision, parse_message_with_evidence


def test_raw_counterfactual_replay_is_deterministic(raw_factory):
    messages = [
        raw_factory("QQQ 719C TODAY @ 1.01", message_id="entry"),
        raw_factory("Closing runners 19%", message_id="exit"),
    ]
    first = run_counterfactual(messages)
    second = run_counterfactual(messages)
    assert first.fingerprint == second.fingerprint
    assert first.effects == second.effects


def test_parser_version_identity_change_is_not_semantic_state_change(raw_factory):
    messages = [raw_factory("QQQ 719C TODAY @ 1.01", message_id="entry")]

    def versioned_identity(raw, allowed):
        parsed = parse_message_with_evidence(raw, allowed)
        return ParseDecision(
            replace(
                parsed.event,
                event_id=f"candidate-{parsed.event.event_id}",
                parser_version="candidate",
            ),
            parsed.evidence,
        )

    baseline = run_counterfactual(messages)
    candidate = run_counterfactual(messages, parser=versioned_identity)
    delta = compare_counterfactual_runs(baseline, candidate)
    assert delta.fingerprint_changed is False
    assert delta.event_kind_changes == 0
    assert delta.contract_changes == 0


def test_counterfactual_detects_candidate_action_change(raw_factory):
    messages = [raw_factory("Printing +19%", message_id="pct")]

    def aggressive(raw, allowed):
        parsed = parse_message_with_evidence(raw, allowed)
        event = parsed.event
        if event.kind is EventKind.AMBIGUOUS:
            event = replace(event, kind=EventKind.EXIT)
        return ParseDecision(event, parsed.evidence)

    baseline = run_counterfactual(messages)
    candidate = run_counterfactual(messages, parser=aggressive)
    delta = compare_counterfactual_runs(baseline, candidate)
    assert delta.event_kind_changes == 1
    assert delta.effect_changes >= 1


def test_counterfactual_effect_delta_preserves_duplicate_multiplicity():
    trim = Effect(EffectKind.PROPOSE_TRIM, "AAPL|CALL|200|2026-09-08", "e", 1, "source_trim")
    baseline = CounterfactualRun("same", (), (trim,), 0, 0)
    candidate = CounterfactualRun("same", (), (trim, trim), 0, 0)
    delta = compare_counterfactual_runs(baseline, candidate)
    assert delta.fingerprint_changed is False
    assert delta.effect_changes == 1

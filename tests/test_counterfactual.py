from scorpion.counterfactual import compare_counterfactual_runs, run_counterfactual
from scorpion.domain import EventKind
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


def test_counterfactual_detects_candidate_action_change(raw_factory):
    messages = [raw_factory("Printing +19%", message_id="pct")]

    def aggressive(raw, allowed):
        parsed = parse_message_with_evidence(raw, allowed)
        event = parsed.event
        if event.kind is EventKind.AMBIGUOUS:
            event = type(event)(
                **{
                    **{field: getattr(event, field) for field in event.__dataclass_fields__},
                    "kind": EventKind.EXIT,
                }
            )
        return ParseDecision(event, parsed.evidence)

    baseline = run_counterfactual(messages)
    candidate = run_counterfactual(messages, parser=aggressive)
    delta = compare_counterfactual_runs(baseline, candidate)
    assert delta.event_kind_changes == 1
    assert delta.effect_changes >= 1

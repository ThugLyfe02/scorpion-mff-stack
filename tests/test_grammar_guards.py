from scorpion.adversarial import evaluate_grammar_robustness
from scorpion.domain import EventKind
from scorpion.parser import parse_message


def test_conditional_entry_fails_closed(raw_factory):
    event = parse_message(raw_factory("If confirmed, QQQ 719C TODAY @ 1.01"))
    assert event.kind is EventKind.AMBIGUOUS
    assert event.reason == "conditional_action_context"


def test_explicit_action_grammar_mutations_do_not_leak(raw_factory):
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    report = evaluate_grammar_robustness(raw, parse_message)
    assert report.total >= 5
    assert report.action_leaks == 0

import pytest

from scorpion.adversarial import evaluate_grammar_robustness
from scorpion.domain import EventKind
from scorpion.parser import parse_message


def test_conditional_entry_fails_closed(raw_factory):
    event = parse_message(raw_factory("If confirmed, QQQ 719C TODAY @ 1.01"))
    assert event.kind is EventKind.AMBIGUOUS
    assert event.reason == "conditional_action_context"


@pytest.mark.parametrize(
    "text",
    (
        "Didn't buy QQQ 719C TODAY @ 1.01",
        "Never added QQQ 719C TODAY @ 1.01",
        "Not buying QQQ 719C TODAY @ 1.01",
        "Not closed yet",
    ),
)
def test_common_negated_action_inflections_fail_closed(raw_factory, text):
    event = parse_message(raw_factory(text))
    assert event.kind is EventKind.AMBIGUOUS
    assert event.reason == "negated_action_language"


@pytest.mark.parametrize(
    "text",
    (
        "Maybe taking profits",
        "Planning to take some",
        "Yesterday averaged here",
        "Earlier locking some",
        "Maybe scaling out",
    ),
)
def test_context_guards_cover_every_action_family(raw_factory, text):
    event = parse_message(raw_factory(text))
    assert event.kind is EventKind.AMBIGUOUS
    assert event.reason in {"conditional_action_context", "historical_action_context"}


def test_short_leading_prose_cannot_become_ticker(raw_factory):
    event = parse_message(raw_factory("Alert QQQ 719C TODAY @ 1.01"))
    assert event.kind is EventKind.ENTRY
    assert event.ticker == "QQQ"
    assert event.contract_key == "QQQ|CALL|719|2026-09-08"


def test_explicit_action_grammar_mutations_do_not_leak(raw_factory):
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    report = evaluate_grammar_robustness(raw, parse_message)
    assert report.total >= 5
    assert report.action_leaks == 0

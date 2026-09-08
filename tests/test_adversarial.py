from scorpion.adversarial import evaluate_surface_robustness
from scorpion.domain import EventKind
from scorpion.parser import parse_message


def test_entry_is_stable_under_presentation_mutations(raw_factory):
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    report = evaluate_surface_robustness(raw, parse_message)
    assert report.total >= 5
    assert report.kind_changes == 0
    assert report.contract_changes == 0


def test_negated_close_is_not_actionable(raw_factory):
    event = parse_message(raw_factory("Not closing runners yet 19%"))
    assert event.kind is EventKind.AMBIGUOUS


def test_do_not_buy_contract_is_not_entry(raw_factory):
    event = parse_message(raw_factory("Do not buy QQQ 719C TODAY @ 1.01"))
    assert event.kind is EventKind.AMBIGUOUS


def test_historical_close_is_not_current_exit(raw_factory):
    event = parse_message(raw_factory("Closed QQQ earlier at 19%"))
    assert event.kind is EventKind.AMBIGUOUS

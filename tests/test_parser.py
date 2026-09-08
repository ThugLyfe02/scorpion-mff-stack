from scorpion.domain import EventKind
from scorpion.parser import parse_message


def test_entry_is_deterministic(raw_factory):
    event = parse_message(raw_factory("QQQ 719C TODAY @ 1.01"))
    assert event.kind is EventKind.ENTRY
    assert event.ticker == "QQQ"
    assert event.option_side == "CALL"
    assert str(event.strike) == "719"
    assert str(event.referenced_price) == "1.01"


def test_nke_is_excluded(raw_factory):
    event = parse_message(raw_factory("NKE 42.5C Sep 11 @ 1.40"))
    assert event.kind is EventKind.IGNORE
    assert event.reason == "excluded_ticker"


def test_percent_without_action_is_ambiguous(raw_factory):
    event = parse_message(raw_factory("Printing +19%"))
    assert event.kind is EventKind.AMBIGUOUS


def test_explicit_exit(raw_factory):
    event = parse_message(raw_factory("Closing runners 19%+"))
    assert event.kind is EventKind.EXIT


def test_wrong_channel_ignored(raw_factory):
    event = parse_message(raw_factory("QQQ 719C TODAY @ 1.01", channel_id="not-allowed"))
    assert event.kind is EventKind.IGNORE

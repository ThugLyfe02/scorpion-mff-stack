from scorpion.association import associate_followup_with_evidence
from scorpion.domain import BookState, PositionState, PositionStatus
from scorpion.parser import parse_message
from scorpion.reducer import reduce_book


def _open_two(raw_factory):
    state = BookState()
    first = parse_message(raw_factory("QQQ 719C TODAY @ 1.01", message_id="e1"))
    state, _ = reduce_book(state, first)
    second = parse_message(
        raw_factory(
            "NVDA 227.5P Sep11 @ 2.63",
            message_id="e2",
            channel_id="968352649437126676",
        )
    )
    state, _ = reduce_book(state, second)
    return state


def test_explicit_ticker_resolves_among_two_live(raw_factory):
    state = _open_two(raw_factory)
    followup = parse_message(raw_factory("QQQ close at 10%", message_id="f1"))
    decision = associate_followup_with_evidence(followup, state)
    assert decision.event.ticker == "QQQ"
    assert decision.evidence.method == "explicit_ticker"
    assert decision.evidence.confidence >= 0.95


def test_source_lineage_resolves_when_ticker_absent(raw_factory):
    state = _open_two(raw_factory)
    followup = parse_message(raw_factory("Closing runners 19%+", message_id="f2"))
    decision = associate_followup_with_evidence(followup, state)
    assert decision.event.ticker == "QQQ"
    assert decision.evidence.method == "source_lineage"


def test_ambiguous_lineage_refuses_guess(raw_factory):
    position_a = PositionState(
        "QQQ|CALL|719|2026-09-08",
        status=PositionStatus.OPEN,
        generation=1,
        source_entry_message_id="a",
        source_channel_id="1231301953972207667",
        source_author_id="author",
    )
    position_b = PositionState(
        "SPY|CALL|650|2026-09-08",
        status=PositionStatus.OPEN,
        generation=1,
        source_entry_message_id="b",
        source_channel_id="1231301953972207667",
        source_author_id="author",
    )
    state = BookState(
        positions={position_a.contract_key: position_a, position_b.contract_key: position_b}
    )
    followup = parse_message(raw_factory("Closing runners +10%", message_id="f3"))
    decision = associate_followup_with_evidence(followup, state)
    assert decision.event.contract_key is None
    assert decision.evidence.method == "source_lineage_ambiguous"
    assert decision.evidence.candidate_count == 2


def test_reply_reference_is_strongest(raw_factory):
    state = _open_two(raw_factory)
    key = "NVDA|PUT|227.5|2026-09-11"
    followup = parse_message(raw_factory("Closing runners 19%+", message_id="f4"))
    decision = associate_followup_with_evidence(followup, state, key)
    assert decision.event.contract_key == key
    assert decision.evidence.method == "reply_reference"
    assert decision.evidence.confidence == 1.0

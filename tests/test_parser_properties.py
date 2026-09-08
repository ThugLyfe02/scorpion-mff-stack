from hypothesis import given
from hypothesis import strategies as st

from scorpion.domain import EventKind
from scorpion.parser import parse_message

ACTIONABLE = {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT}


@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=100))
def test_untrusted_author_can_never_create_action(text, raw_factory):
    event = parse_message(
        raw_factory(text, author_id="untrusted"),
        allowed_author_ids=frozenset({"trusted"}),
    )
    assert event.kind not in ACTIONABLE
    assert event.reason == "author_not_allowed"


@given(st.integers(min_value=1, max_value=99))
def test_excluded_ticker_can_never_become_entry(cents, raw_factory):
    event = parse_message(raw_factory(f"NKE 42.5C Sep11 @ 1.{cents:02d}"))
    assert event.kind is EventKind.IGNORE
    assert event.reason == "excluded_ticker"


@given(st.sampled_from([" ", "  ", "\n", "\t"]))
def test_whitespace_variants_preserve_contract(separator, raw_factory):
    text = separator.join(["QQQ", "719C", "TODAY", "@", "1.01"])
    event = parse_message(raw_factory(text))
    assert event.kind is EventKind.ENTRY
    assert event.contract_key == "QQQ|CALL|719|2026-09-08"

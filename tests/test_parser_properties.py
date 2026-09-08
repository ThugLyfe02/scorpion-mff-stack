from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from scorpion.domain import EventKind, RawDiscordMessage
from scorpion.parser import parse_message

ACTIONABLE = {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT}


def _raw(
    content: str,
    *,
    author_id: str = "author",
    channel_id: str = "1231301953972207667",
) -> RawDiscordMessage:
    ts = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    return RawDiscordMessage(
        message_id="property",
        guild_id="912747256736800838",
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        source_ts_utc=ts,
        received_ts_utc=ts,
    )


@given(text=st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=100))
def test_untrusted_author_can_never_create_action(text):
    event = parse_message(
        _raw(text, author_id="untrusted"),
        allowed_author_ids=frozenset({"trusted"}),
    )
    assert event.kind not in ACTIONABLE
    assert event.reason == "author_not_allowed"


@given(cents=st.integers(min_value=1, max_value=99))
def test_excluded_ticker_can_never_become_entry(cents):
    event = parse_message(_raw(f"NKE 42.5C Sep11 @ 1.{cents:02d}"))
    assert event.kind is EventKind.IGNORE
    assert event.reason == "excluded_ticker"


@given(separator=st.sampled_from([" ", "  ", "\n", "\t"]))
def test_whitespace_variants_preserve_contract(separator):
    text = separator.join(["QQQ", "719C", "TODAY", "@", "1.01"])
    event = parse_message(_raw(text))
    assert event.kind is EventKind.ENTRY
    assert event.contract_key == "QQQ|CALL|719|2026-09-08"

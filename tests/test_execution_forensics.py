from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.execution_forensics import (
    ForensicStatus,
    run_execution_forensics,
)
from scorpion.history_archive import ArchivedDiscordMessage
from scorpion.quote_tape import HistoricalQuote, HistoricalQuoteTape

OPTIONS_KING = "968352649437126676"
HIGH_CONFIDENCE = "1448448931116748993"
GUILD = "912747256736800838"
AUTHOR = "author"


def _message(
    message_id: str,
    content: str,
    ts: datetime,
    *,
    channel_id: str = OPTIONS_KING,
    referenced_message_id: str | None = None,
) -> ArchivedDiscordMessage:
    return ArchivedDiscordMessage(
        message_id=message_id,
        guild_id=GUILD,
        channel_id=channel_id,
        author_id=AUTHOR,
        source_ts_utc=ts,
        referenced_message_id=referenced_message_id,
        content=content,
    )


def test_exact_execution_uses_ask_on_entry_and_bid_on_exit():
    start = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    key = "TSLA|CALL|345|2026-09-08"
    messages = (
        _message("1", "TSLA 345C TODAY @ 1.00", start),
        _message("2", "closing runners 20%", start + timedelta(minutes=1)),
    )
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(
                key,
                start + timedelta(milliseconds=250),
                Decimal("1.00"),
                Decimal("1.05"),
            ),
            HistoricalQuote(
                key,
                start + timedelta(minutes=1, milliseconds=250),
                Decimal("1.25"),
                Decimal("1.30"),
            ),
        )
    )
    legs, completed = run_execution_forensics(
        messages,
        tape,
        allowed_author_ids=frozenset({AUTHOR}),
    )
    assert [leg.status for leg in legs] == [ForensicStatus.FILLED, ForensicStatus.FILLED]
    assert len(completed) == 1
    trade = completed[0]
    assert trade.initial_quantity == 1
    assert trade.gross_premium_in == Decimal("105.00")
    assert trade.gross_proceeds == Decimal("125.00")
    assert trade.pnl == Decimal("20.00")
    assert trade.return_fraction == Decimal("20") / Decimal("105")


def test_entry_over_25_percent_dislocation_is_skipped():
    start = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    key = "TSLA|CALL|345|2026-09-08"
    messages = (_message("1", "TSLA 345C TODAY @ 1.00", start),)
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(
                key,
                start + timedelta(milliseconds=250),
                Decimal("1.25"),
                Decimal("1.30"),
            ),
        )
    )
    legs, completed = run_execution_forensics(
        messages,
        tape,
        allowed_author_ids=frozenset({AUTHOR}),
    )
    assert legs[0].status is ForensicStatus.STALE_ENTRY
    assert completed == ()


def test_limit_between_15_and_25_percent_requires_actual_trade_back():
    start = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    key = "TSLA|CALL|345|2026-09-08"
    messages = (_message("1", "TSLA 345C TODAY @ 1.00", start),)
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(
                key,
                start + timedelta(milliseconds=250),
                Decimal("1.18"),
                Decimal("1.20"),
            ),
            HistoricalQuote(
                key,
                start + timedelta(seconds=6),
                Decimal("1.10"),
                Decimal("1.12"),
            ),
        )
    )
    legs, completed = run_execution_forensics(
        messages,
        tape,
        allowed_author_ids=frozenset({AUTHOR}),
    )
    assert legs[0].status is ForensicStatus.LIMIT_NOT_FILLED
    assert legs[0].limit_price == Decimal("1.1500")
    assert completed == ()


def test_etf_is_preserved_but_not_executed_by_default():
    start = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    messages = (
        _message(
            "1",
            "QQQ 719C TODAY @ 1.01",
            start,
            channel_id=HIGH_CONFIDENCE,
        ),
    )
    legs, completed = run_execution_forensics(
        messages,
        HistoricalQuoteTape(()),
        allowed_author_ids=frozenset({AUTHOR}),
    )
    assert legs[0].status is ForensicStatus.RESEARCH_ONLY
    assert legs[0].note == "etf_underlying_research_only"
    assert completed == ()

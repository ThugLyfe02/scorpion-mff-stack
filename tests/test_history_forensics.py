import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.execution_forensics import ForensicStatus, run_execution_forensics
from scorpion.history_archive import ArchivedDiscordMessage, HistoryArchive
from scorpion.quote_tape import HistoricalQuote, HistoricalQuoteTape


GUILD = "912747256736800838"
CHANNEL = "968352649437126676"
AUTHOR = "author"


def _archived(
    content: str,
    *,
    message_id: str,
    ts: datetime,
    edited_ts: datetime | None = None,
) -> ArchivedDiscordMessage:
    return ArchivedDiscordMessage(
        message_id=message_id,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=AUTHOR,
        source_ts_utc=ts,
        edited_ts_utc=edited_ts,
        content=content,
    )


def test_history_archive_is_idempotent_and_preserves_later_revision(tmp_path):
    archive = HistoryArchive(tmp_path / "history.db")
    ts = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    original = _archived("AAPL 200C TODAY @ 1.00", message_id="1", ts=ts)
    edited = _archived(
        "AAPL 200C TODAY @ 1.05",
        message_id="1",
        ts=ts,
        edited_ts=ts + timedelta(seconds=3),
    )

    assert archive.append(original) is True
    assert archive.append(original) is False
    assert archive.append(edited) is True
    archive.mark_channel_synced(CHANNEL, reached_beginning=True)

    completeness = archive.completeness(CHANNEL)
    assert completeness.revision_count == 2
    assert completeness.unique_message_count == 1
    assert completeness.exhaustive is True
    messages = archive.iter_channel(CHANNEL)
    assert len(messages) == 1
    assert messages[0].content.endswith("1.05")


def test_history_archive_migrates_v1_message_table(tmp_path):
    path = tmp_path / "legacy-history.db"
    ts = datetime(2026, 9, 8, 14, 0, tzinfo=UTC).isoformat()
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE historical_discord_messages (
                message_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                source_ts_utc TEXT NOT NULL,
                edited_ts_utc TEXT,
                referenced_message_id TEXT,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                archived_ts_utc TEXT NOT NULL
            )
            """
        )
        content = "AAPL 200C TODAY @ 1.00"
        import hashlib

        digest = hashlib.sha256(content.encode()).hexdigest()
        db.execute(
            "INSERT INTO historical_discord_messages VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("1", GUILD, CHANNEL, AUTHOR, ts, None, None, content, digest, ts),
        )
        db.commit()

    archive = HistoryArchive(path)
    messages = archive.iter_channel(CHANNEL)
    assert len(messages) == 1
    assert messages[0].message_id == "1"
    with archive.connect() as db:
        columns = {
            str(row[1])
            for row in db.execute("PRAGMA table_info(historical_discord_messages)")
        }
    assert "revision_id" in columns


def test_quote_tape_never_uses_predecision_quote_and_waits_for_limit():
    contract = "AAPL|CALL|200|2026-09-08"
    target = datetime(2026, 9, 8, 14, 0, 0, 250000, tzinfo=UTC)
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(
                contract,
                target - timedelta(milliseconds=100),
                Decimal("0.99"),
                Decimal("1.00"),
            ),
            HistoricalQuote(
                contract,
                target + timedelta(milliseconds=50),
                Decimal("1.18"),
                Decimal("1.20"),
            ),
            HistoricalQuote(
                contract,
                target + timedelta(seconds=2),
                Decimal("1.13"),
                Decimal("1.14"),
            ),
        )
    )
    first = tape.at_or_after(contract, target)
    assert first is not None
    assert first.ask == Decimal("1.20")
    fill = tape.first_ask_at_or_below(contract, target, Decimal("1.15"))
    assert fill is not None
    assert fill.ask == Decimal("1.14")


def test_execution_forensics_uses_quote_side_fills_and_real_source_exit_only():
    opened = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    closed = opened + timedelta(minutes=1)
    messages = (
        _archived("AAPL 200C TODAY @ 1.00", message_id="1", ts=opened),
        _archived("closing runners", message_id="2", ts=closed),
    )
    contract = "AAPL|CALL|200|2026-09-08"
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(
                contract,
                opened + timedelta(milliseconds=300),
                Decimal("1.00"),
                Decimal("1.05"),
            ),
            HistoricalQuote(
                contract,
                closed + timedelta(milliseconds=300),
                Decimal("1.20"),
                Decimal("1.25"),
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
    assert trade.gross_proceeds == Decimal("120.00")
    assert trade.pnl == Decimal("15.00")
    assert trade.return_fraction == Decimal("15") / Decimal("105")


def test_execution_forensics_skips_entry_when_live_ask_is_over_25_percent():
    opened = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    contract = "AAPL|CALL|200|2026-09-08"
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(
                contract,
                opened + timedelta(milliseconds=300),
                Decimal("1.28"),
                Decimal("1.30"),
            ),
        )
    )
    legs, completed = run_execution_forensics(
        (_archived("AAPL 200C TODAY @ 1.00", message_id="1", ts=opened),),
        tape,
        allowed_author_ids=frozenset({AUTHOR}),
    )
    assert legs[0].status is ForensicStatus.STALE_ENTRY
    assert completed == ()

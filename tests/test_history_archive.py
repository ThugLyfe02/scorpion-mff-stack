from datetime import UTC, datetime, timedelta

from scorpion.history_archive import ArchivedDiscordMessage, HistoryArchive


def test_history_archive_dedupes_and_tracks_beginning(tmp_path):
    archive = HistoryArchive(tmp_path / "history.db")
    start = datetime(2022, 3, 9, 14, 0, tzinfo=UTC)
    first = ArchivedDiscordMessage(
        message_id="1",
        guild_id="guild",
        channel_id="channel",
        author_id="author",
        source_ts_utc=start,
        content="first",
    )
    second = ArchivedDiscordMessage(
        message_id="2",
        guild_id="guild",
        channel_id="channel",
        author_id="author",
        source_ts_utc=start + timedelta(days=1),
        content="second",
        referenced_message_id="1",
    )
    assert archive.append(first) is True
    assert archive.append(first) is False
    assert archive.append(second) is True
    archive.mark_channel_synced("channel", reached_beginning=True)

    completeness = archive.completeness("channel")
    assert completeness.message_count == 2
    assert completeness.exhaustive is True
    assert completeness.oldest_source_ts_utc == start
    assert completeness.newest_source_ts_utc == start + timedelta(days=1)
    messages = archive.iter_channel("channel")
    assert [message.message_id for message in messages] == ["1", "2"]
    assert messages[1].referenced_message_id == "1"


def test_history_archive_does_not_claim_exhaustive_before_checkpoint(tmp_path):
    archive = HistoryArchive(tmp_path / "history.db")
    assert archive.completeness("missing").exhaustive is False

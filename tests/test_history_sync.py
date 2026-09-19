import sqlite3

import pytest

from scorpion.history_archive import HistoryArchive
from scorpion.history_sync import DiscordHistorySynchronizer


class _FailingClient:
    def __init__(self, *, intents):
        self._closed = False

    def event(self, callback):
        return callback

    async def start(self, token):
        raise RuntimeError("authentication failed")

    async def close(self):
        self._closed = True

    def is_closed(self):
        return self._closed


@pytest.mark.asyncio
async def test_history_sync_startup_failure_finalizes_run(tmp_path, monkeypatch):
    path = tmp_path / "history-sync.db"
    archive = HistoryArchive(path)
    monkeypatch.setattr("scorpion.history_sync.discord.Client", _FailingClient)

    synchronizer = DiscordHistorySynchronizer(
        archive,
        channel_ids=frozenset({"968352649437126676"}),
    )
    with pytest.raises(RuntimeError, match="authentication failed"):
        await synchronizer.sync("bad-token")

    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT status,completed_ts_utc,note FROM history_sync_runs"
        ).fetchone()
    assert row is not None
    assert row[0] == "FAILED"
    assert row[1] is not None
    assert "authentication failed" in row[2]

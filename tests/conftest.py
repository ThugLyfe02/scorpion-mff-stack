from datetime import UTC, datetime

import pytest

from scorpion.domain import RawDiscordMessage


@pytest.fixture
def raw_factory():
    def make(
        content: str,
        *,
        message_id: str = "1",
        channel_id: str = "1231301953972207667",
        guild_id: str = "912747256736800838",
        author_id: str = "author",
        hour: int = 14,
    ) -> RawDiscordMessage:
        ts = datetime(2026, 9, 8, hour, 0, tzinfo=UTC)
        return RawDiscordMessage(
            message_id=message_id,
            guild_id=guild_id,
            channel_id=channel_id,
            author_id=author_id,
            content=content,
            source_ts_utc=ts,
            received_ts_utc=ts,
        )
    return make

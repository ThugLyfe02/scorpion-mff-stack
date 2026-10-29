"""Shared fixtures for the control-plane tests and the live-lane tests.

The `sys.path` bootstrap makes the repo root importable without an install step
(added by `main`); `raw_factory` is the control plane's message builder. Merging
the two lanes must not drop either, so this file is the union, not a choice.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scorpion.domain import RawDiscordMessage

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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

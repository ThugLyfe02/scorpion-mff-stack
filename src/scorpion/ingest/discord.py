from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import discord

from ..config import ALLOWED_CHANNEL_IDS, GUILD_ID
from ..domain import RawDiscordMessage

Sink = Callable[[RawDiscordMessage], Awaitable[None]]


class DiscordSignalClient(discord.Client):
    """Push-only Discord ingest with revision-aware edit capture.

    This component has no broker dependency and must run in a separate process from chat/research
    workloads. Both creates and edits are emitted as immutable raw revisions.
    """

    def __init__(self, sink: Sink) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self._sink = sink

    async def on_ready(self) -> None:
        return None

    async def _emit(self, message: discord.Message) -> None:
        if message.guild is None or str(message.guild.id) != GUILD_ID:
            return
        if str(message.channel.id) not in ALLOWED_CHANNEL_IDS:
            return
        source = message.created_at.astimezone(UTC)
        edited = message.edited_at.astimezone(UTC) if message.edited_at else None
        ref = (
            str(message.reference.message_id)
            if message.reference and message.reference.message_id
            else None
        )
        raw = RawDiscordMessage(
            message_id=str(message.id),
            guild_id=str(message.guild.id),
            channel_id=str(message.channel.id),
            author_id=str(message.author.id),
            content=message.content,
            source_ts_utc=source,
            received_ts_utc=datetime.now(UTC),
            edited_ts_utc=edited,
            referenced_message_id=ref,
        )
        await self._sink(raw)

    async def on_message(self, message: discord.Message) -> None:
        await self._emit(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        del before
        await self._emit(after)

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import discord

from ..config import ALLOWED_CHANNEL_IDS, CHANNELS, GUILD_ID
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
        # Read-only health evidence. Readiness is not proof of complete Discord history.
        self.first_forward_scope_ready_ts_utc: datetime | None = None
        self.first_disconnect_ts_utc: datetime | None = None
        self.callback_failures = 0

    async def on_ready(self) -> None:
        guild = self.get_guild(int(GUILD_ID))
        if (
            self.first_forward_scope_ready_ts_utc is None
            and guild is not None
            and guild.get_channel(int(CHANNELS["options-king"])) is not None
        ):
            self.first_forward_scope_ready_ts_utc = datetime.now(UTC)

    async def on_disconnect(self) -> None:
        if self.first_disconnect_ts_utc is None:
            self.first_disconnect_ts_utc = datetime.now(UTC)

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

    async def _emit_with_health(self, message: discord.Message) -> None:
        try:
            await self._emit(message)
        except Exception:
            self.callback_failures += 1
            raise

    async def on_message(self, message: discord.Message) -> None:
        await self._emit_with_health(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        del before
        await self._emit_with_health(after)

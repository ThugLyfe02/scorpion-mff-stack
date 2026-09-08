from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass

import discord

from .config import ALLOWED_CHANNEL_IDS, GUILD_ID
from .history_archive import ArchivedDiscordMessage, HistoryArchive

_BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class HistorySyncResult:
    channels: int
    messages_seen: int
    messages_inserted: int
    textless_messages: int
    embed_messages: int


def _visible_text(message: discord.Message) -> str:
    """Capture text visible in message content and Discord embeds without OCR guessing."""
    parts: list[str] = []
    if message.content.strip():
        parts.append(message.content.strip())
    for embed in message.embeds:
        if embed.author and embed.author.name:
            parts.append(embed.author.name)
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            if field.name:
                parts.append(field.name)
            if field.value:
                parts.append(field.value)
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
    return "\n".join(part for part in parts if part.strip())


class DiscordHistorySynchronizer:
    """Read-only authorized Discord history reader; never sends/edits/reacts."""

    def __init__(
        self,
        archive: HistoryArchive,
        *,
        guild_id: str = GUILD_ID,
        channel_ids: frozenset[str] = ALLOWED_CHANNEL_IDS,
        batch_size: int = _BATCH_SIZE,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.archive = archive
        self.guild_id = guild_id
        self.channel_ids = channel_ids
        self.batch_size = batch_size

    async def sync(self, token: str) -> HistorySyncResult:
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        seen = 0
        inserted = 0
        textless = 0
        embed_messages = 0
        run_id = self.archive.start_run(len(self.channel_ids))

        @client.event
        async def on_ready() -> None:
            nonlocal seen, inserted, textless, embed_messages
            try:
                for channel_id in sorted(self.channel_ids):
                    channel = client.get_channel(int(channel_id))
                    if channel is None:
                        channel = await client.fetch_channel(int(channel_id))
                    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                        raise RuntimeError(f"channel {channel_id} is not text-readable")
                    if channel.guild is None or str(channel.guild.id) != self.guild_id:
                        raise RuntimeError(f"channel {channel_id} does not belong to configured guild")

                    batch: list[ArchivedDiscordMessage] = []
                    # Traverse from the beginning. `reached_beginning` is only set after the
                    # iterator completes normally; repeated audits are revision-idempotent.
                    async for message in channel.history(limit=None, oldest_first=True):
                        seen += 1
                        embed_messages += int(bool(message.embeds))
                        reference_id = (
                            str(message.reference.message_id)
                            if message.reference is not None
                            and message.reference.message_id is not None
                            else None
                        )
                        text = _visible_text(message)
                        textless += int(not text)
                        batch.append(
                            ArchivedDiscordMessage(
                                message_id=str(message.id),
                                guild_id=str(message.guild.id),
                                channel_id=str(channel.id),
                                author_id=str(message.author.id),
                                source_ts_utc=message.created_at,
                                edited_ts_utc=message.edited_at,
                                referenced_message_id=reference_id,
                                content=text,
                            )
                        )
                        if len(batch) >= self.batch_size:
                            inserted += self.archive.append_many(batch)
                            batch.clear()
                    if batch:
                        inserted += self.archive.append_many(batch)
                    self.archive.mark_channel_synced(channel_id, reached_beginning=True)

                self.archive.finish_run(
                    run_id,
                    messages_seen=seen,
                    messages_inserted=inserted,
                    note=(
                        f"textless_messages={textless}; embed_messages={embed_messages}; "
                        "attachments are preserved by Discord but image text is not OCR-guessed"
                    ),
                )
            except Exception as exc:
                self.archive.finish_run(
                    run_id,
                    messages_seen=seen,
                    messages_inserted=inserted,
                    status="FAILED",
                    note=f"{type(exc).__name__}: {exc}",
                )
                raise
            finally:
                await client.close()

        await client.start(token)
        return HistorySyncResult(
            len(self.channel_ids),
            seen,
            inserted,
            textless,
            embed_messages,
        )


def history_sync_main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only exhaustive Discord channel history sync using an authorized bot token."
    )
    parser.add_argument("--archive", default="scorpion-history.db")
    parser.add_argument(
        "--channel-id",
        action="append",
        dest="channel_ids",
        help="Repeat to override the default four MFF source channels.",
    )
    parser.add_argument("--batch-size", type=int, default=_BATCH_SIZE)
    args = parser.parse_args()
    token = os.environ.get("SCORPION_DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("SCORPION_DISCORD_TOKEN is required")
    channel_ids = frozenset(args.channel_ids or ALLOWED_CHANNEL_IDS)
    result = asyncio.run(
        DiscordHistorySynchronizer(
            HistoryArchive(args.archive),
            channel_ids=channel_ids,
            batch_size=args.batch_size,
        ).sync(token)
    )
    print(
        f"channels={result.channels} seen={result.messages_seen} "
        f"inserted={result.messages_inserted} textless={result.textless_messages} "
        f"embed_messages={result.embed_messages}"
    )

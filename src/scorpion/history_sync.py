from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass

import discord

from .config import ALLOWED_CHANNEL_IDS, GUILD_ID
from .history_archive import ArchivedDiscordMessage, HistoryArchive


@dataclass(frozen=True, slots=True)
class HistorySyncResult:
    channels: int
    messages_seen: int
    messages_inserted: int


class DiscordHistorySynchronizer:
    """Legitimate bot-token history reader; never sends, edits, or reacts to Discord messages."""

    def __init__(
        self,
        archive: HistoryArchive,
        *,
        guild_id: str = GUILD_ID,
        channel_ids: frozenset[str] = ALLOWED_CHANNEL_IDS,
    ) -> None:
        self.archive = archive
        self.guild_id = guild_id
        self.channel_ids = channel_ids

    async def sync(self, token: str) -> HistorySyncResult:
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        seen = 0
        inserted = 0
        run_id = self.archive.start_run(len(self.channel_ids))

        @client.event
        async def on_ready() -> None:
            nonlocal seen, inserted
            try:
                for channel_id in sorted(self.channel_ids):
                    channel = client.get_channel(int(channel_id))
                    if channel is None:
                        channel = await client.fetch_channel(int(channel_id))
                    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                        raise RuntimeError(f"channel {channel_id} is not text-readable")

                    # Intentionally traverse from the beginning. Deduplication makes repeated
                    # full audits safe, while reached_beginning only becomes true after the
                    # iterator completes without error.
                    async for message in channel.history(limit=None, oldest_first=True):
                        seen += 1
                        reference_id = (
                            str(message.reference.message_id)
                            if message.reference is not None
                            and message.reference.message_id is not None
                            else None
                        )
                        archived = ArchivedDiscordMessage(
                            message_id=str(message.id),
                            guild_id=str(message.guild.id) if message.guild else self.guild_id,
                            channel_id=str(channel.id),
                            author_id=str(message.author.id),
                            source_ts_utc=message.created_at,
                            edited_ts_utc=message.edited_at,
                            referenced_message_id=reference_id,
                            content=message.content,
                        )
                        inserted += int(self.archive.append(archived))
                    self.archive.mark_channel_synced(channel_id, reached_beginning=True)

                self.archive.finish_run(
                    run_id,
                    messages_seen=seen,
                    messages_inserted=inserted,
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
        return HistorySyncResult(len(self.channel_ids), seen, inserted)


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
    args = parser.parse_args()
    token = os.environ.get("SCORPION_DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("SCORPION_DISCORD_TOKEN is required")
    channel_ids = frozenset(args.channel_ids or ALLOWED_CHANNEL_IDS)
    result = asyncio.run(
        DiscordHistorySynchronizer(
            HistoryArchive(args.archive),
            channel_ids=channel_ids,
        ).sync(token)
    )
    print(
        f"channels={result.channels} seen={result.messages_seen} "
        f"inserted={result.messages_inserted}"
    )

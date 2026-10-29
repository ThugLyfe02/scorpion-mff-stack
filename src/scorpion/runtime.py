from __future__ import annotations

import asyncio
import signal

from .config import RuntimeSettings
from .domain import RawDiscordMessage
from .ingest.discord import DiscordSignalClient
from .pipeline import Pipeline
from .store import Store


async def run_discord() -> None:
    settings = RuntimeSettings.from_env()
    store = Store(settings.database_path)
    pipeline = Pipeline(store=store, allowed_author_ids=settings.allowed_author_ids)

    async def sink(raw: RawDiscordMessage) -> None:
        await pipeline.handle(raw)

    client = DiscordSignalClient(sink)
    loop = asyncio.get_running_loop()

    async def shutdown() -> None:
        store.heartbeat("discord-ingest", status="stopping")
        await client.close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    store.heartbeat("discord-ingest", status="starting")
    await client.start(settings.discord_token)


def ingest_main() -> None:
    asyncio.run(run_discord())

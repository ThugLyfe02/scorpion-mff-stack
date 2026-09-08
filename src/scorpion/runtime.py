from __future__ import annotations

import asyncio
import contextlib
import signal

from .config import RuntimeSettings
from .domain import RawDiscordMessage
from .ingest.discord import DiscordSignalClient
from .pipeline import Pipeline
from .resilience_watch import ResilienceController
from .store import Store


async def run_discord() -> None:
    settings = RuntimeSettings.from_env()
    store = Store(settings.database_path)
    controller = ResilienceController(
        store=store,
        allowed_author_ids=settings.allowed_author_ids,
    )
    controller.refresh()
    pipeline = Pipeline(
        store=store,
        allowed_author_ids=settings.allowed_author_ids,
        resilience_assessment=controller.current(),
        source_shift_resolver=controller.source_shift,
    )

    async def sink(raw: RawDiscordMessage) -> None:
        pipeline.resilience_assessment = controller.current()
        await pipeline.handle(raw)

    client = DiscordSignalClient(sink)
    loop = asyncio.get_running_loop()
    watchdog_task = asyncio.create_task(controller.run(), name="resilience-watchdog")

    async def shutdown() -> None:
        store.heartbeat("discord-ingest", status="stopping")
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task
        await client.close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    store.heartbeat("discord-ingest", status="starting")
    await client.start(settings.discord_token)


def ingest_main() -> None:
    asyncio.run(run_discord())

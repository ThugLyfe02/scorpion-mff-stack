from __future__ import annotations

import asyncio
import contextlib
import os
import signal

from .config import RuntimeSettings
from .domain import RawDiscordMessage
from .ingest.discord import DiscordSignalClient
from .pipeline import Pipeline
from .resilience_watch import ResilienceController
from .store import Store


def _queue_capacity() -> int:
    raw = os.environ.get("SCORPION_INGRESS_QUEUE_MAX", "512").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("SCORPION_INGRESS_QUEUE_MAX must be an integer") from exc
    if value <= 0:
        raise ValueError("SCORPION_INGRESS_QUEUE_MAX must be positive")
    return value


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
    ingress: asyncio.Queue[RawDiscordMessage] = asyncio.Queue(maxsize=_queue_capacity())

    async def sink(raw: RawDiscordMessage) -> None:
        # Cross the lossless boundary before waiting on any stateful parser/reducer work.
        store.append_raw(raw)
        await ingress.put(raw)
        store.heartbeat(
            "discord-ingress-queue",
            status="queued",
            depth=ingress.qsize(),
            capacity=ingress.maxsize,
            utilization=ingress.qsize() / ingress.maxsize,
            last_revision_id=raw.revision_id,
        )

    async def consume_ingress() -> None:
        while True:
            raw = await ingress.get()
            try:
                pipeline.resilience_assessment = controller.current()
                await pipeline.handle_persisted(raw)
                store.heartbeat(
                    "discord-ingress-queue",
                    status="processed",
                    depth=ingress.qsize(),
                    capacity=ingress.maxsize,
                    utilization=ingress.qsize() / ingress.maxsize,
                    last_revision_id=raw.revision_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The raw revision is already durable. Later queued revisions must not advance
                # normalized state ahead of this unresolved transition. Stop this consumer and
                # leave the queue plus raw_processing evidence for deterministic recovery.
                store.set_halt(True, f"live_transition_failed:{raw.revision_id}")
                pipeline.resilience_assessment = controller.refresh()
                store.heartbeat(
                    "discord-ingress-queue",
                    status="halted",
                    depth=ingress.qsize(),
                    capacity=ingress.maxsize,
                    utilization=ingress.qsize() / ingress.maxsize,
                    last_revision_id=raw.revision_id,
                    error=type(exc).__name__,
                )
                return
            finally:
                ingress.task_done()

    client = DiscordSignalClient(sink)
    loop = asyncio.get_running_loop()
    watchdog_task = asyncio.create_task(controller.run(), name="resilience-watchdog")
    consumer_task = asyncio.create_task(consume_ingress(), name="discord-ingress-consumer")

    async def shutdown() -> None:
        store.heartbeat("discord-ingest", status="stopping", queue_depth=ingress.qsize())
        await client.close()
        # Give already-durable messages a short opportunity to finish. On timeout they remain in
        # raw_processing and are deterministically recovered by Pipeline startup.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ingress.join(), timeout=5.0)
        consumer_task.cancel()
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer_task
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    store.heartbeat(
        "discord-ingest",
        status="starting",
        ingress_queue_capacity=ingress.maxsize,
    )
    await client.start(settings.discord_token)


def ingest_main() -> None:
    asyncio.run(run_discord())

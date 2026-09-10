from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time

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


class _HeartbeatPublisher:
    """Coalesce diagnostic heartbeats so they do not contend with the transition writer."""

    def __init__(self, store: Store, *, minimum_interval_seconds: float = 0.25) -> None:
        if minimum_interval_seconds <= 0:
            raise ValueError("minimum_interval_seconds must be positive")
        self.store = store
        self.minimum_interval_seconds = minimum_interval_seconds
        self._last_emitted: dict[str, float] = {}
        self._last_status: dict[str, str] = {}

    async def publish(
        self,
        component: str,
        *,
        status: str,
        force: bool = False,
        **metadata: object,
    ) -> bool:
        now = time.monotonic()
        previous = self._last_emitted.get(component)
        status_changed = self._last_status.get(component) != status
        due = previous is None or now - previous >= self.minimum_interval_seconds
        if not force and not status_changed and not due:
            return False
        self._last_emitted[component] = now
        self._last_status[component] = status
        await asyncio.to_thread(
            self.store.heartbeat,
            component,
            status=status,
            **metadata,
        )
        return True


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
    durable_write_lock = asyncio.Lock()
    heartbeats = _HeartbeatPublisher(store)
    consumer_task: asyncio.Task[None] | None = None
    shutdown_started = asyncio.Event()

    async def sink(raw: RawDiscordMessage) -> None:
        # Keep the gateway loop responsive while preserving serialized durable receipt writes.
        async with durable_write_lock:
            inserted = await asyncio.to_thread(store.append_raw, raw)
        if not inserted:
            await heartbeats.publish(
                "discord-ingress-queue",
                status="duplicate_receipt",
                depth=ingress.qsize(),
                capacity=ingress.maxsize,
                utilization=ingress.qsize() / ingress.maxsize,
                last_revision_id=raw.revision_id,
            )
            return

        # If normalized processing has halted, continue lossless capture without filling a dead
        # in-memory queue. Startup recovery will rebuild receipt order and process the durable tail.
        if consumer_task is not None and consumer_task.done():
            await heartbeats.publish(
                "discord-ingress-queue",
                status="capture_only",
                depth=ingress.qsize(),
                capacity=ingress.maxsize,
                utilization=ingress.qsize() / ingress.maxsize,
                last_revision_id=raw.revision_id,
                consumer_alive=False,
            )
            return

        await ingress.put(raw)
        await heartbeats.publish(
            "discord-ingress-queue",
            status="queued",
            depth=ingress.qsize(),
            capacity=ingress.maxsize,
            utilization=ingress.qsize() / ingress.maxsize,
            last_revision_id=raw.revision_id,
            consumer_alive=True,
        )

    async def consume_ingress() -> None:
        while True:
            raw = await ingress.get()
            try:
                pipeline.resilience_assessment = controller.current()
                await pipeline.handle_persisted(raw)
                await heartbeats.publish(
                    "discord-ingress-queue",
                    status="processed",
                    depth=ingress.qsize(),
                    capacity=ingress.maxsize,
                    utilization=ingress.qsize() / ingress.maxsize,
                    last_revision_id=raw.revision_id,
                    consumer_alive=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The raw revision is already durable. Stop normalized mutation and leave all
                # remaining receipts recoverable while the gateway continues capture-only mode.
                await asyncio.to_thread(
                    store.set_halt,
                    True,
                    f"live_transition_failed:{raw.revision_id}",
                )
                pipeline.resilience_assessment = await asyncio.to_thread(controller.refresh)
                await heartbeats.publish(
                    "discord-ingress-queue",
                    status="halted",
                    force=True,
                    depth=ingress.qsize(),
                    capacity=ingress.maxsize,
                    utilization=ingress.qsize() / ingress.maxsize,
                    last_revision_id=raw.revision_id,
                    consumer_alive=False,
                    error=type(exc).__name__,
                )
                return
            finally:
                ingress.task_done()

    async def publish_ingress_liveness() -> None:
        while True:
            alive = consumer_task is not None and not consumer_task.done()
            await heartbeats.publish(
                "discord-ingress-queue",
                status="healthy" if alive else "capture_only",
                force=True,
                depth=ingress.qsize(),
                capacity=ingress.maxsize,
                utilization=ingress.qsize() / ingress.maxsize,
                consumer_alive=alive,
            )
            await asyncio.sleep(1.0)

    client = DiscordSignalClient(sink)
    loop = asyncio.get_running_loop()
    watchdog_task = asyncio.create_task(controller.run(), name="resilience-watchdog")
    consumer_task = asyncio.create_task(consume_ingress(), name="discord-ingress-consumer")
    liveness_task = asyncio.create_task(
        publish_ingress_liveness(),
        name="discord-ingress-liveness",
    )

    async def shutdown() -> None:
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        await heartbeats.publish(
            "discord-ingest",
            status="stopping",
            force=True,
            queue_depth=ingress.qsize(),
        )
        await client.close()
        # Already-durable messages get a short drain window. Anything left remains in
        # raw_processing and is deterministically recovered at the next startup.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ingress.join(), timeout=5.0)
        consumer_task.cancel()
        watchdog_task.cancel()
        liveness_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer_task
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task
        with contextlib.suppress(asyncio.CancelledError):
            await liveness_task

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    await heartbeats.publish(
        "discord-ingest",
        status="starting",
        force=True,
        ingress_queue_capacity=ingress.maxsize,
    )
    await client.start(settings.discord_token)


def ingest_main() -> None:
    asyncio.run(run_discord())

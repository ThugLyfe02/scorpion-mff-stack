"""Non-blocking PagerDuty Events API v2 pager for the paper session.

``page()`` only enqueues; a worker task sends with a 2 s timeout and up to 3 retries. A failed
page logs a warning and never blocks or raises into the decision loop. The routing key comes from
the environment (``SCORPION_PAGERDUTY_ROUTING_KEY``) and is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)
EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
ROUTING_KEY_ENV = "SCORPION_PAGERDUTY_ROUTING_KEY"

Transport = Callable[[dict[str, object]], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class Page:
    dedup_key: str
    summary: str
    source: str = "scorpion-paper"
    severity: str = "critical"


def pagerduty_payload(page: Page, routing_key: str) -> dict[str, object]:
    severity = page.severity.lower()
    if severity not in {"critical", "error", "warning", "info"}:
        raise ValueError("PagerDuty severity must be critical/error/warning/info")
    return {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": page.dedup_key,
        "payload": {
            "summary": page.summary[:1024],
            "source": page.source,
            "severity": severity,
        },
    }


async def urllib_transport(body: dict[str, object], *, timeout_s: float = 2.0) -> int:
    data = json.dumps(body).encode("utf-8")

    def send() -> int:
        request = urllib.request.Request(
            EVENTS_URL, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return int(response.status)

    return await asyncio.to_thread(send)


class Pager:
    def __init__(
        self,
        *,
        routing_key: str | None = None,
        transport: Transport | None = None,
        retries: int = 3,
        timeout_s: float = 2.0,
        retry_delay_s: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._routing_key = (
            routing_key if routing_key is not None else os.environ.get(ROUTING_KEY_ENV, "")
        )
        self._transport = transport
        self._retries = retries
        self._timeout_s = timeout_s
        self._retry_delay_s = retry_delay_s
        self._sleep = sleep
        self._queue: asyncio.Queue[Page] = asyncio.Queue(maxsize=256)
        self._seen: set[str] = set()
        self.sent: list[str] = []
        self.failed: list[str] = []

    @property
    def enqueued(self) -> int:
        return len(self._seen)

    def page(self, page: Page) -> bool:
        """Enqueue once per dedup_key. Returns False for a duplicate or a full queue."""

        if page.dedup_key in self._seen:
            return False
        self._seen.add(page.dedup_key)
        try:
            self._queue.put_nowait(page)
        except asyncio.QueueFull:
            LOGGER.warning("page queue full; dropping page dedup_key=%s", page.dedup_key)
            return False
        LOGGER.warning("PAGE %s: %s", page.dedup_key, page.summary)
        return True

    def pending(self) -> int:
        return self._queue.qsize()

    async def _send(self, page: Page) -> bool:
        if not self._routing_key:
            LOGGER.warning("no PagerDuty routing key configured; page %s logged only", page.dedup_key)
            return False
        body = pagerduty_payload(page, self._routing_key)
        for attempt in range(1, self._retries + 1):
            try:
                if self._transport is not None:
                    status = await asyncio.wait_for(self._transport(body), self._timeout_s)
                else:
                    status = await urllib_transport(body, timeout_s=self._timeout_s)
                if 200 <= status < 300:
                    return True
                LOGGER.warning("page %s attempt %d HTTP %d", page.dedup_key, attempt, status)
            except Exception as exc:  # never propagate into the session
                LOGGER.warning(
                    "page %s attempt %d failed: %s", page.dedup_key, attempt, type(exc).__name__
                )
            await self._sleep(self._retry_delay_s * attempt)
        return False

    async def drain_once(self) -> None:
        page = await self._queue.get()
        try:
            ok = await self._send(page)
            (self.sent if ok else self.failed).append(page.dedup_key)
        finally:
            self._queue.task_done()

    async def run(self) -> None:
        while True:
            await self.drain_once()

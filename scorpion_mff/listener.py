"""Event-driven listener: Discord Gateway -> parser -> engine -> broker.

This replaces the cron+wake+browser loop that lost 31 minutes on 9/8. There is
no agent turn, no shared chat queue and no scheduler: a websocket event is
handled inline and the decision latency is measured per event.

Transports:
  * TapeTransport      - replay a recorded tape (offline, deterministic)
  * WebsocketGateway   - the real Discord Gateway (needs the optional
                         `websockets` package; imported lazily so every other
                         path keeps running on the standard library alone)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .broker import BrokerError, QuoteProvider
from .engine import Engine
from .fills import FillLog
from .health import Heartbeat, LatencyBudget
from .killswitch import KillSwitch
from .parser import CHANNEL_IDS, parse
from .types import OrderIntent, RawMessage

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
INTENTS_GUILD_MESSAGES = 1 << 9          # GUILD_MESSAGES
INTENT_MESSAGE_CONTENT = 1 << 15         # MESSAGE_CONTENT (privileged)


class GatewayTransport(Protocol):
    def messages(self) -> AsyncIterator[RawMessage]: ...


@dataclass
class TapeTransport:
    """Yields recorded messages. delay_s=0 replays as fast as the CPU allows."""

    tape: list[RawMessage]
    delay_s: float = 0.0

    async def messages(self) -> AsyncIterator[RawMessage]:
        for msg in self.tape:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            yield msg


@dataclass
class Listener:
    """Wires the pipeline together and keeps the audit trail honest."""

    transport: GatewayTransport
    engine: Engine
    broker: Any                                   # Broker protocol
    quotes: QuoteProvider
    budget: LatencyBudget = field(default_factory=LatencyBudget)
    heartbeat: Heartbeat = field(default_factory=Heartbeat)
    kill: KillSwitch = field(default_factory=KillSwitch)
    fills: FillLog | None = None
    clock: Callable[[], float] = time.time
    processed: int = 0
    seen: dict[str, None] = field(default_factory=dict)   # message_id -> nothing
    seen_max: int = 5000                                  # bounded; oldest evicted

    def __post_init__(self) -> None:
        if self.fills is None:
            self.fills = self.engine.fills
        self.engine.blocked = self.kill.blocked     # one switch, checked per buy
        self.engine.quotes = self.quotes            # the engine prices its own
                                                    # decisions, incl. exits that
                                                    # do not restate the contract

    async def run(self, limit: int | None = None) -> int:
        async for message in self.transport.messages():
            if limit is not None and self.processed >= limit:
                break
            self.handle(message)
        return self.processed

    def handle(self, message: RawMessage) -> list[OrderIntent]:
        """One event, handled end to end, synchronously. Returns the intents."""
        started = self.clock()

        # A Gateway RESUME can redeliver a MESSAGE_CREATE. Without this the same
        # post trades twice - and the second buy carries a fresh idempotency key,
        # so the broker cannot catch it either.
        mid = message.message_id
        if mid:
            if mid in self.seen:
                if self.fills is not None:
                    self.fills.record(event="duplicate", contract_key="", qty=0, price=0.0,
                                      reason="redelivered_message_id", ts=self.clock(),
                                      accepted=False, note=mid)
                return []
            self.seen[mid] = None
            while len(self.seen) > self.seen_max:
                self.seen.pop(next(iter(self.seen)))

        signal = parse(message)
        # No pre-fetch here: the engine resolves the target position first and
        # prices THAT, so an exit post which does not restate the contract is
        # still priced off what it actually resolves to.
        intents = self.engine.on_signal(signal)

        for intent in intents:
            self._place(intent)

        latency_ms = int((self.clock() - started) * 1000)
        self.budget.record(latency_ms)
        self.heartbeat.beat(note=f"msg:{signal.kind.value}")
        if self.fills is not None:
            self.fills.record(
                event="latency", contract_key=signal.contract_key, qty=0, price=0.0,
                reason=f"decision:{signal.kind.value}", ts=self.clock(),
                latency_ms=latency_ms)
        self.processed += 1
        return intents

    def _place(self, intent: OrderIntent) -> None:
        """Place one intent and record the ack or the refusal, never raising."""
        try:
            ack = self.broker.place(intent)
        except BrokerError as exc:
            self.engine.fills.record(
                event="reject", contract_key=intent.contract_key, qty=intent.qty,
                price=intent.limit, reason=f"broker:{exc}", accepted=False,
                ts=self.clock(), note=intent.reason)
            return
        if self.fills is not None:
            self.fills.record(
                event="ack", contract_key=ack.contract_key, qty=ack.qty,
                price=ack.price, reason=intent.reason, ts=self.clock(),
                note=f"{'dry' if ack.dry_run else 'live'}:{ack.broker_order_id}")

    def refresh_stops(self) -> list[OrderIntent]:
        """Poll quotes for open positions and fire any armed breakeven stop.

        A no-op unless the engine opted in (`EngineConfig.enforce_breakeven`). The
        live pump calls this on its cadence so "trim to 1 + BE" is enforced between
        Discord events, not only when the next post arrives. A quote feed failure
        skips the position - fail closed, never guess a fill.
        """
        if not self.engine.config.enforce_breakeven:
            return []
        placed: list[OrderIntent] = []
        for position in self.engine.open_positions:
            try:
                quote = self.quotes.quote(position.contract_key)
            except Exception:      # QuoteError and friends: unpriceable != priced
                continue
            if quote is None:
                continue
            for intent in self.engine.on_quote(position.contract_key, quote):
                self._place(intent)
                placed.append(intent)
        return placed

    @property
    def breached_budget(self) -> bool:
        return self.budget.breaches > 0


# ── the real Gateway ─────────────────────────────────────────────────────────
@dataclass
class WebsocketGateway:
    """Discord Gateway v10 client: IDENTIFY, heartbeat, RESUME, MESSAGE_CREATE.

    Only the four allowlisted channel ids are yielded upward; every other guild
    event is dropped before it can reach the parser.
    """

    token: str
    channel_ids: Iterable[str] = field(default_factory=lambda: tuple(CHANNEL_IDS.values()))
    url: str = GATEWAY_URL
    heartbeat_margin: float = 0.1
    name_to_id: dict[str, str] = field(default_factory=lambda: dict(CHANNEL_IDS))

    async def messages(self) -> AsyncIterator[RawMessage]:
        try:
            import websockets  # optional dependency
        except ImportError as exc:                 # pragma: no cover - env dependent
            raise RuntimeError(
                "the live Gateway needs the optional dependency: pip install websockets"
            ) from exc

        allowed = {str(c) for c in self.channel_ids}
        async with websockets.connect(self.url, max_size=2**23) as ws:
            hello = json.loads(await ws.recv())
            interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
            await ws.send(json.dumps({
                "op": 2,
                "d": {
                    "token": self.token,
                    "intents": INTENTS_GUILD_MESSAGES | INTENT_MESSAGE_CONTENT,
                    "properties": {"os": "windows", "browser": "scorpion-mff",
                                   "device": "scorpion-mff"},
                },
            }))
            beat = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                async for frame in ws:
                    event = json.loads(frame)
                    if event.get("op") != 0 or event.get("t") != "MESSAGE_CREATE":
                        continue
                    payload = event.get("d") or {}
                    channel_id = str(payload.get("channel_id", ""))
                    if channel_id not in allowed:
                        continue
                    yield RawMessage(
                        channel=self._name_for(channel_id),
                        channel_id=channel_id,
                        text=str(payload.get("content", "")),
                        message_id=str(payload.get("id", "")),
                        ts=time.time(),
                    )
            finally:
                beat.cancel()

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        while True:
            await asyncio.sleep(max(1.0, interval * (1 - self.heartbeat_margin)))
            try:
                await ws.send(json.dumps({"op": 1, "d": None}))
            except Exception:  # noqa: BLE001 - the reader owns the failure path
                return

    def _name_for(self, channel_id: str) -> str:
        for name, cid in self.name_to_id.items():
            if cid == channel_id:
                return name
        return channel_id

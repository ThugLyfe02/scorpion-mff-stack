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
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .broker import BrokerError, QuoteProvider
from .engine import Engine
from .execution import (
    STATE_PARTIAL,
    STATE_PENDING,
    ExecutionBook,
    ExecutionError,
)
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
    execution: ExecutionBook | None = None
    clock: Callable[[], float] = time.time
    _lock: Any = field(default_factory=threading.RLock)
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
        """One event, serialized against the protective-exit poller."""
        with self._lock:
            return self._handle_locked(message)

    def _handle_locked(self, message: RawMessage) -> list[OrderIntent]:
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

        trade_ts = self.clock()
        self._place_intents(intents, channel=signal.channel, ts=trade_ts,
                            source_prefix=mid)

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

    def _place_intents(self, intents: list[OrderIntent], *, channel: str,
                       ts: float, source_prefix: str = "") -> None:
        """Record, place and confirm each intent under the current lock."""
        for intent in intents:
            source_id = f"{source_prefix}:{intent.contract_key}" if source_prefix else ""
            cid = ""
            if self.execution is not None:
                cid = self.execution.client_id(intent, ts, source_id)
                existing = self.execution.orders.get(cid)
                if existing is not None:
                    # A restart clears the in-memory `seen` ring. The durable
                    # client id is tied to the Discord message id, so a redelivered
                    # message maps to the order it already produced and is never
                    # placed twice.
                    self.engine.fills.record(
                        event="duplicate", contract_key=intent.contract_key,
                        qty=0, price=0.0, reason="execution_source_already_recorded",
                        ts=ts, accepted=False, note=source_id or intent.contract_key)
                    continue
                try:
                    cid = self.execution.record_intent(
                        intent, ts=ts, channel=channel, source_id=source_id).client_id
                except ExecutionError as exc:
                    # No durable intent means we must not place the order.
                    self.engine.fills.record(
                        event="alert", contract_key=intent.contract_key, qty=intent.qty,
                        price=intent.limit, reason=f"execution_intent:{exc}",
                        ts=ts, accepted=False, note=intent.reason)
                    continue
            try:
                ack = self.broker.place(intent)
            except BrokerError as exc:
                self._reject_intent(intent, cid, f"broker:{exc}", ts)
                continue
            if not ack.ok:
                self._reject_intent(intent, cid, ack.error or "broker_rejected", ts)
                continue
            if self.execution is not None and cid:
                try:
                    self.execution.apply_ack(cid, ack, ts=ts)
                    self.execution.sync_engine(self.engine, ts)
                except ExecutionError as exc:
                    # The broker filled, but the book cannot represent it. Re-sync
                    # whatever did land, halt new buys (exits still run), and make
                    # the desync impossible to miss.
                    self.execution.sync_engine(self.engine, ts)
                    self.engine.halted = True
                    self.engine.fills.record(
                        event="alert", contract_key=intent.contract_key,
                        qty=ack.qty, price=ack.price,
                        reason=f"execution_desync:{exc}", ts=ts,
                        accepted=False, note=intent.reason)
                    continue
            if self.fills is not None:
                note = f"{'dry' if ack.dry_run else 'live'}:{ack.broker_order_id}"
                if ack.qty < intent.qty:
                    note += f":partial:{ack.qty}/{intent.qty}"
                self.fills.record(
                    event="ack", contract_key=ack.contract_key, qty=ack.qty,
                    price=ack.price, reason=intent.reason, ts=ts, note=note)

    def enforce_stops(self, now: float | None = None) -> list[OrderIntent]:
        """Poll the breakeven stop for trimmed runners and place any exit.

        Called from a background task in `arm`; the listener lock serializes it
        against the Discord handler, so the engine book is never mutated by both.
        A stop that is already PENDING/PARTIAL is not re-sent; a rejected one is
        retried in the next 15-second bucket (the idempotency key changes).
        """
        with self._lock:
            pending_keys: set[str] = set()
            if self.execution is not None:
                for order in self.execution.orders.values():
                    if (order.state in (STATE_PENDING, STATE_PARTIAL)
                            and order.intent.reason.startswith("stop:be")):
                        pending_keys.add(order.contract_key)
            ts = self.clock() if now is None else now
            intents = [i for i in self.engine.protective_exits(now=ts)
                       if i.contract_key not in pending_keys]
            if intents:
                self._place_intents(intents, channel="", ts=ts,
                                    source_prefix=f"be:{int(ts) // 15}")
            return intents

    def _reject_intent(self, intent: OrderIntent, cid: str, reason: str,
                       ts: float) -> None:
        """Record a failed order and undo the engine's optimistic mutation."""
        if self.execution is not None and cid:
            try:
                self.execution.reject(cid, reason=reason, ts=ts)
                self.execution.sync_engine(self.engine, ts)
            except ExecutionError as exc:
                # A rejection we cannot represent is a desync: stop new buys.
                self.engine.halted = True
                reason = f"{reason};execution:{exc}"
        log = self.fills if self.fills is not None else self.engine.fills
        log.record(event="reject", contract_key=intent.contract_key, qty=intent.qty,
                   price=intent.limit, reason=reason, accepted=False, ts=ts,
                   note=intent.reason)

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
                    "properties": {"os": "windows", "browser": "scorpion-mff", "device": "scorpion-mff"},
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

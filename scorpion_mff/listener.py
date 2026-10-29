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

from .broker import BrokerError, QuoteProvider, TransportError, intent_key
from .engine import Engine
from .execution import BreakerState, CircuitBreaker
from .fills import FillLog
from .health import Heartbeat, LatencyBudget
from .killswitch import KillSwitch
from .ledger import TRANSPORT_UNKNOWN
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
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    processed: int = 0
    seen: dict[str, None] = field(default_factory=dict)   # message_id -> nothing
    seen_max: int = 5000                                  # bounded; oldest evicted
    # The clock-driven cadence of the pump: while the channel is quiet, the only clock
    # this stack has is the one it schedules itself. 15s is shorter than the 15-minute
    # expiry window by two orders of magnitude, so the cutoff cannot be missed by traffic.
    tick_s: float = 15.0

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
        """Record the intent, place it, then record the terminal ack or reject.

        The intent row is written BEFORE the broker call so a crash between the
        two leaves a row the ledger can see: an intent with no terminal. The
        idempotency key travels in the note, so the ledger can match the intent
        to its terminal even after the process died and came back.

        The circuit breaker gates BUYS only: while it is open, a new entry is
        refused before it reaches the venue, but an exit is always attempted -
        the kill-switch invariant (you must be able to leave a position you are
        in) survives a transport outage.
        """
        key = intent_key(intent)
        now = self.clock()
        if self.fills is not None:
            self.fills.record(
                event="intent", contract_key=intent.contract_key, qty=intent.qty,
                price=intent.limit, reason=intent.reason, accepted=True,
                ts=now, note=key)
        if intent.action == "buy" and not self.breaker.allow(now):
            if self.fills is not None:
                self.fills.record(
                    event="reject", contract_key=intent.contract_key, qty=intent.qty,
                    price=intent.limit, reason="breaker_open", accepted=False,
                    ts=now, note=key)
            return
        try:
            ack = self.broker.place(intent)
        except TransportError as exc:
            self._on_transport_failure(intent, key, now, exc)
            return
        except BrokerError as exc:
            # A business reject is the venue answering: transport is working, so
            # it breaks the streak of consecutive transport failures rather than
            # tripping the breaker.
            self.breaker.record_success()
            if self.fills is not None:
                self.fills.record(
                    event="reject", contract_key=intent.contract_key, qty=intent.qty,
                    price=intent.limit, reason=f"broker:{exc}", accepted=False,
                    ts=now, note=key)
            return
        self.breaker.record_success()
        if self.fills is not None:
            self.fills.record(
                event="ack", contract_key=ack.contract_key, qty=ack.qty,
                price=ack.price, reason=intent.reason, ts=now,
                note=key)

    def _on_transport_failure(self, intent: OrderIntent, key: str, now: float,
                              exc: TransportError) -> None:
        """Quarantine an order the venue may or may not have: never fake a terminal.

        A transport error is not a rejection. It means the socket died and the answer
        is unknown, so writing `reject` here would be a lie in the one direction that
        costs money: the engine books a buy at intent time (a failed buy stays in the
        book, which is conservative) but deletes a position at intent time (a failed
        sell leaves the local book flat while the venue may still hold the risk).
        A terminal would also hide the order from `recover`, which is the only tool
        that can settle it.

        So the intent stays OPEN with a `transport_unknown` row naming the failure.
        `verify` flags it, `recover` resolves it from a venue lookup (SEEN/NOT_SEEN;
        CANNOT_SAY keeps it open), and nothing retries. On top of that:

          * the breaker records the failure and pages once when it opens; and
          * a transport-failed EXIT pages immediately (`exit_unknown:transport`),
            because that is the asymmetric case where real risk can be remote while
            this process believes it is flat. The breaker never gates exits, so this
            is the exit-side alarm.
        """
        was_open = self.breaker.state is BreakerState.OPEN
        self.breaker.record_failure(now)
        if self.fills is not None:
            reason = f"transport:{exc}"
            self.fills.record(
                event=TRANSPORT_UNKNOWN, contract_key=intent.contract_key,
                qty=intent.qty, price=intent.limit, reason=reason, accepted=False,
                ts=now, note=key)
            if self.breaker.state is BreakerState.OPEN and not was_open:
                # One alert per open transition: a page, not a per-tick stream.
                self.fills.record(
                    event="alert", contract_key="", qty=0, price=0.0,
                    reason="breaker_open", accepted=False, ts=now,
                    note=f"threshold={self.breaker.failure_threshold} "
                         f"cooldown={self.breaker.cooldown_seconds:g}s")
            if intent.action != "buy":
                self.fills.record(
                    event="alert", contract_key=intent.contract_key,
                    qty=intent.qty, price=intent.limit,
                    reason="exit_unknown:transport", accepted=False, ts=now,
                    note=f"{key}: {reason}; local book is flat, venue state is unknown")

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

    # ── the clock, made live ─────────────────────────────────────────────────
    def tick(self, now: float | None = None,
             underlying: dict[str, float] | None = None) -> list[OrderIntent]:
        """One clock-driven pass, between Discord events. Returns what it placed.

        Everything in this stack used to be triggered by a post, which quietly assumes
        that the channel is always talking. At 15:45 ET it is not: the last thing the
        channel posted is the entry, and the one event that must happen - flatten the
        0DTE before the bell turns it into shares - has no post behind it. So the pump
        calls this on a timeout, and the expiry guard gets a clock of its own.

        Three things happen here, in this order and for this reason:

          1. quotes for the OPEN book are polled fresh (fail closed: a position whose
             quote cannot be read is simply absent from the map, which the guard reads
             as SILENCE and pages, never as a fill);
          2. `Engine.expiry_intents` classifies and acts - force-close at a live bid, or
             a deduped HALT_AND_PAGE with projected assignment shares (or `unknown`);
          3. the armed breakeven stops are re-checked, because "trim to 1 + BE" must not
             depend on someone posting afterwards either.

        Placement reuses `_place`, so an ack/refusal lands in the same log as an
        event-driven order and the audit trail cannot tell the two apart - which is what
        you want when the question is "did it actually flatten?".
        """
        ts = self.clock() if now is None else now
        placed: list[OrderIntent] = []
        for intent in self.engine.expiry_intents(ts, self._bids_for_book(), underlying):
            self._place(intent)
            placed.append(intent)
        placed.extend(self.refresh_stops())
        self.heartbeat.beat(note="tick:expiry")
        return placed

    def _bids_for_book(self) -> dict[str, float]:
        """A bid per open contract, or no entry at all. Never a guessed price."""
        bids: dict[str, float] = {}
        for position in self.engine.open_positions:
            try:
                quote = self.quotes.quote(position.contract_key)
            except Exception:      # QuoteError and friends: unpriceable != priced
                continue
            if quote is None:
                continue
            bids[position.contract_key] = quote.bid
        return bids


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

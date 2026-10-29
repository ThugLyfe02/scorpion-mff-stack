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
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

from .broker import Ack, BrokerError, QuoteProvider, TransportError, intent_key
from .engine import Engine
from .execution import BreakerState, CircuitBreaker
from .execution_book import ExecutionBook
from .fills import FillLog
from .gateway import (
    GATEWAY_URL,
    NORMAL_CLOSE_CODE,
    ZOMBIE_CLOSE_CODE,
    CloseAction,
    GatewayFatalError,
    GatewaySession,
)
from .health import Heartbeat, LatencyBudget
from .killswitch import KillSwitch
from .ledger import TRANSPORT_UNKNOWN
from .parity import Trace
from .parser import CHANNEL_IDS, parse
from .reconcile import ReconcileReport, VenuePosition
from .types import OrderIntent, RawMessage
from .venue import VenueError, VenueFeed


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
    # The confirmed book, when attached: intents and venue answers are folded into a
    # durable state that survives a restart. It is optional so every offline path
    # (replay, fuzz, the corpus) keeps running on the fill log alone.
    execution: ExecutionBook | None = None
    processed: int = 0
    seen: dict[str, None] = field(default_factory=dict)   # message_id -> nothing
    seen_max: int = 5000                                  # bounded; oldest evicted
    # The clock-driven cadence of the pump: while the channel is quiet, the only clock
    # this stack has is the one it schedules itself. 15s is shorter than the 15-minute
    # expiry window by two orders of magnitude, so the cutoff cannot be missed by traffic.
    tick_s: float = 15.0
    # Scheduled venue reconciliation. Off unless a feed is attached; when it is, the
    # tick asks the venue for its book every `venue_every_s` and the engine halts new
    # buys on drift. 300s is five ticks: often enough to catch a manual trade before a
    # new entry, cheap enough that a positions endpoint is not hammered.
    venue_feed: VenueFeed | None = None
    venue_every_s: float = 300.0
    venue_checks: int = 0
    last_venue_check: float = 0.0
    # Per order, the confirmed P&L already folded into the engine. Without it a
    # redelivered ack would apply the same correction twice; with it the second ack of
    # a partial applies only the new contracts. Keyed by the book's client id.
    aligned_orders: dict[str, float] = field(default_factory=dict)
    # An optional decision trace (`parity.py`). When attached, every handled message
    # contributes one record built from the objects this method already produced, so a
    # real session can be diffed against the offline replay event by event. Off by
    # default: tracing is an evidence act, not a side effect of running.
    trace: Trace | None = None

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
        if self.trace is not None and self.fills is not None:
            # Recorded after the latency row so one event contributes one complete slice
            # of the log. A trace must never change a decision, so this call reads and
            # appends nothing to the book: `Trace` is a recorder, not a participant.
            self.trace.record(message=message, signal=signal, intents=intents,
                              engine=self.engine, rows=self.fills.records)
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
        # The confirmed book records what was SENT, not what was considered: the
        # breaker refusal above never reaches a venue, so it has no place in a book
        # of confirmed execution. From here on, every outcome is folded in.
        booked = self._book_intent(intent, key, now)
        try:
            ack = self.broker.place(intent)
        except TransportError as exc:
            self._on_transport_failure(intent, key, now, exc, booked)
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
            self._book_reject(intent, booked, f"broker:{exc}", now)
            return
        self.breaker.record_success()
        if self.fills is not None:
            self.fills.record(
                event="ack", contract_key=ack.contract_key, qty=ack.qty,
                price=ack.price, reason=intent.reason, ts=now,
                note=key)
        self._book_ack(intent, booked, ack, now)

    def _on_transport_failure(self, intent: OrderIntent, key: str, now: float,
                              exc: TransportError, booked: str = "") -> None:
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
        self._book_unknown(intent, booked, f"transport:{exc}", now)

    # ── confirmed execution: the book behind the fill log ─────────────────────
    #
    # These four methods are the only place the listener touches the execution book.
    # Each failure is turned into an `alert` row instead of an exception: the book is
    # the durable record, but it is not the venue. A bug in bookkeeping must not abort
    # an order that has already been sent, and it must not vanish either - `watch`
    # pages on engine alerts, so a book that cannot write is a page, not a silence.
    def _book_alert(self, action: str, exc: Exception) -> None:
        if self.fills is None:
            return
        self.fills.record(
            event="alert", contract_key="", qty=0, price=0.0,
            reason=f"execution_book:{action}:{exc}", accepted=False, ts=self.clock(),
            note="the fill log still holds the venue answer; the confirmed book does not")

    def _book_intent(self, intent: OrderIntent, key: str, now: float) -> str:
        """Book the intent before the send; return the client id the venue answers on."""
        execution = self.execution
        if execution is None:
            return ""
        channel = ""
        position = self.engine.positions.get(intent.contract_key)
        if position is not None:
            channel = position.channel
        try:
            return execution.record_intent(intent, ts=now, channel=channel,
                                           source_id=key).client_id
        except Exception as exc:            # noqa: BLE001 - surfaced as an alert row
            self._book_alert("intent", exc)
            return ""

    def _book_ack(self, intent: OrderIntent, booked: str, ack: Ack, now: float) -> None:
        """Fold the venue's cumulative answer into the book (no-op without a book).

        Then make the RUNNING engine agree with it. `sync_engine` already did this at
        arm time, but the process that booked an exit kept its optimistic view until
        the next restart - so a sell the venue had not filled left the bot believing it
        was flat, with `closed` set, and no way to leave. That is the defect this call
        closes; it is deliberately conservative about the two unknowns (see
        `_apply_confirmed_contract`).
        """
        execution = self.execution
        if execution is None or not booked:
            return
        try:
            order = execution.apply_ack(booked, ack, ts=now)
        except Exception as exc:            # noqa: BLE001 - surfaced as an alert row
            self._book_alert("ack", exc)
            return
        # A sell must always be reflected: FILLED removes the lot, PARTIAL/PENDING
        # leaves a remainder. A buy is only applied once the venue says FILLED; a
        # partial or unanswered buy keeps the engine's larger position, because the
        # remainder can still fill and over-counting risk is the safe direction.
        if order.action == "sell":
            self._align_sell_pnl(intent, booked, now)
        self._apply_confirmed_contract(intent.contract_key, now,
                                       apply_position=order.action == "sell"
                                       or order.terminal)

    def _book_reject(self, intent: OrderIntent, booked: str, reason: str,
                     now: float) -> None:
        """A business reject is terminal: the venue answered, and it said no."""
        execution = self.execution
        if execution is None or not booked:
            return
        try:
            execution.reject(booked, reason=reason, ts=now)
        except Exception as exc:            # noqa: BLE001 - surfaced as an alert row
            self._book_alert("reject", exc)
            return
        self._align_sell_pnl(intent, booked, now)
        self._apply_confirmed_contract(intent.contract_key, now, apply_position=True)

    def _book_unknown(self, intent: OrderIntent, booked: str, reason: str,
                      now: float) -> None:
        """A transport failure: the order stays OPEN so `recover` can settle it."""
        execution = self.execution
        if execution is None or not booked:
            return
        try:
            execution.mark_unknown(booked, reason=reason, ts=now)
        except Exception as exc:            # noqa: BLE001 - surfaced as an alert row
            self._book_alert("unknown", exc)
            return
        # Both directions stay conservative about a send the venue may have received:
        # an unknown SELL restores the position (the risk may still be there) and an
        # unknown BUY leaves the engine's position alone (the exposure may be there).
        self._align_sell_pnl(intent, booked, now)
        self._apply_confirmed_contract(intent.contract_key, now,
                                       apply_position=intent.action != "buy")

    def _align_sell_pnl(self, intent: OrderIntent, booked: str, now: float) -> None:
        """Put the engine's realised P&L on the contracts the venue confirmed.

        The engine booked the WHOLE intent at the posted price when it decided. The
        correction is `confirmed - already_applied`, where the first `already_applied`
        is the engine's own assumption and later ones are the previous answers. That
        makes it idempotent under a redelivered ack and correct across partial fills:
        a 2-lot filling 1-then-1 moves the engine by (fill1 + fill2) - assumed, never
        twice.

        A refused or unanswered exit has no confirmed trades, so the whole assumption
        is reversed - while the position is restored to the book. Money never stays
        booked against contracts the engine still holds.
        """
        execution = self.execution
        if execution is None or not booked or intent.action != "sell":
            return
        trades = [trade for trade in execution.realized if trade.client_id == booked]
        entry: float | None = None
        if trades:
            entry = trades[0].entry_price
        else:
            lot = execution.lots.get(intent.contract_key)
            if lot is not None:
                entry = lot.avg_price
        if entry is None:
            return
        confirmed = round(sum(trade.pnl_usd for trade in trades), 2)
        assumed = round((intent.limit - entry) * intent.qty * 100.0, 2)
        applied = self.aligned_orders.get(booked, assumed)
        delta = round(confirmed - applied, 2)
        if not delta:
            self.aligned_orders[booked] = confirmed
            return
        self.engine.align_realized(
            delta, now,
            note=(f"order={booked}:limit={intent.limit:g}:qty={intent.qty}"
                  f":confirmed={confirmed:.2f}:assumed={assumed:.2f}"))
        self.aligned_orders[booked] = confirmed

    def _apply_confirmed_contract(self, contract_key: str, now: float, *,
                                  apply_position: bool) -> None:
        """Make one contract's engine position match the confirmed book.

        Called after every venue answer, so the live process is never more optimistic
        than what the venue confirmed. The asymmetry is the whole design:

          * **sell answered** - apply. A rejected, unfilled or partial sell restores
            the remainder, so the exit path can see it and try again (with a fresh
            idempotency key) instead of believing it is flat.
          * **buy filled or rejected** - apply. A rejected buy must free its slot.
          * **buy partial or unknown** - do NOT shrink. The remainder may still fill,
            and holding a phantom contract costs a slot; holding too few costs real
            money.

        `exit_in_flight` is always refreshed from the book, because that is the only
        authority on whether a sell is still at the venue. A restated position with an
        unanswered sell is visible to marks, the budget and the expiry guard, but no
        guard may send a second exit for it.
        """
        execution = self.execution
        if execution is None or not contract_key:
            return
        before = self.engine.positions.get(contract_key)
        if apply_position:
            after = next((position for position in execution.open_positions()
                          if position.contract_key == contract_key), None)
            if after is None:
                self.engine.positions.pop(contract_key, None)
            else:
                self.engine.positions[contract_key] = after
        self.engine.exit_in_flight = {
            order.contract_key for order in execution.unresolved_orders()
            if order.action == "sell"}
        if not apply_position:
            return
        was = 0 if before is None else before.contracts
        now_contracts = 0
        if contract_key in self.engine.positions:
            now_contracts = self.engine.positions[contract_key].contracts
        if was != now_contracts:
            self.engine.fills.record(
                event="reconcile", contract_key=contract_key, qty=now_contracts,
                price=(self.engine.positions[contract_key].avg_price
                       if contract_key in self.engine.positions else 0.0),
                reason="engine_follows_venue", ts=now, accepted=False,
                note=f"engine_contracts={was}:confirmed={now_contracts}")

    def place(self, intent: OrderIntent) -> None:
        """Place one intent through the live ladder.

        `flatten` uses this so a panic exit is booked, acked, reconciled and P&L-corrected
        by exactly the code an exit post uses. It is a thin public name for `_place`,
        which already owns the intent row, the breaker (buys only), the transport
        quarantine and the confirmed book.
        """
        self._place(intent)

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

          0. the scheduled venue reconcile runs FIRST when a feed is attached, so a book
             that already disagrees with the venue cannot add risk in the same tick;
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
        self.reconcile_from_feed(ts)
        placed: list[OrderIntent] = []
        for intent in self.engine.expiry_intents(ts, self._bids_for_book(), underlying):
            self._place(intent)
            placed.append(intent)
        placed.extend(self.refresh_stops())
        self.heartbeat.beat(note="tick:expiry")
        return placed

    def reconcile(self, venue: Iterable[VenuePosition],
                  now: float | None = None) -> ReconcileReport:
        """Check the engine's book against a venue snapshot.

        Called by whoever holds the venue feed (a positions endpoint, an operator
        upload, a broker callback). This method is deliberately a thin delegate: the
        comparison is pure and tested in its own module, and the engine owns the
        consequence (halt buys, keep exits).
        """
        return self.engine.reconcile_positions(venue, now)

    def reconcile_from_feed(self, now: float | None = None,
                            force: bool = False) -> ReconcileReport | None:
        """Ask the venue for its book, on a cadence, and apply the consequence.

        Returns the report when a snapshot was compared, `None` when no feed is attached,
        when the cadence is not due, or when the venue could not be read. This is the
        only place the live process learns the venue's side, so the three failure classes
        are kept apart on purpose (`venue.py` documents the taxonomy):

        * **unreadable / stale / malformed** - no evidence. An `alert` row names the
          cause so `watch` pages, and buys are **not** halted. Halting on a broken
          adapter would turn an ops bug into a stopped desk, and the human is already
          being told the feed is broken;
        * **drift** - both books read and they disagree. The engine halts new buys and
          pages (deduped by drift signature); exits stay legal.

        The local side is the **confirmed** book when one is attached: the engine deletes
        a position at sell-intent time, so comparing the venue against decisions would
        hide a sell that never arrived.
        """
        feed = self.venue_feed
        if feed is None or not feed.configured:
            return None
        ts = self.clock() if now is None else now
        if not force and self.last_venue_check and ts - self.last_venue_check < self.venue_every_s:
            return None
        self.last_venue_check = ts
        try:
            snapshot = feed.snapshot(ts)
        except (VenueError, ValueError) as exc:
            self._venue_alert(f"reconcile_feed:{exc}", ts,
                              note="no venue evidence this pass; buys are NOT halted - "
                                   "a dead adapter is not a disagreement")
            return None
        self.venue_checks += 1
        local = self.execution.open_positions() if self.execution is not None else None
        try:
            return self.engine.reconcile_positions(snapshot.positions, ts, local=local)
        except ValueError as exc:       # ReconcileError: evidence that is not a book
            self._venue_alert(f"reconcile_rejected:{exc}", ts,
                              note="the snapshot passed the feed and failed the "
                                   "comparator; buys are NOT halted on unusable evidence")
            return None

    def _venue_alert(self, reason: str, ts: float, note: str) -> None:
        """An unreadable venue is a page, never a halt and never a silence."""
        if self.fills is not None:
            self.fills.record(event="alert", contract_key="", qty=0, price=0.0,
                              reason=reason, accepted=False, ts=ts, note=note)

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
    """Discord Gateway v10 client: IDENTIFY, RESUME, heartbeat, MESSAGE_CREATE.

    Only the four allowlisted channel ids are yielded upward; every other guild
    event is dropped before it can reach the parser. A dropped socket reconnects
    with the prior session (RESUME) so alerts posted during the reconnect are
    replayed instead of being lost; a gap, zombie or fatal close is paged.
    """

    token: str
    channel_ids: Iterable[str] = field(default_factory=lambda: tuple(CHANNEL_IDS.values()))
    url: str = GATEWAY_URL
    heartbeat_margin: float = 0.1
    name_to_id: dict[str, str] = field(default_factory=lambda: dict(CHANNEL_IDS))
    alert: Callable[[str, str], None] | None = None
    backoff_s: float = 1.0
    max_backoff_s: float = 30.0

    async def messages(self) -> AsyncIterator[RawMessage]:
        try:
            import websockets  # optional dependency
        except ImportError as exc:                 # pragma: no cover - env dependent
            raise RuntimeError(
                "the live Gateway needs the optional dependency: pip install websockets"
            ) from exc

        session = GatewaySession(token=self.token)
        allowed = {str(c) for c in self.channel_ids}
        delay = self.backoff_s
        while True:
            session.begin()
            action = CloseAction.IDENTIFY
            note = ""
            try:
                url = session.connect_url(self.url)
                async with websockets.connect(url, max_size=2**23) as ws:
                    async for message in self._session(ws, session, allowed):
                        # A healthy stream resets the backoff immediately; a run of
                        # failed reconnects backs off so a Discord outage is not a
                        # tight reconnect loop.
                        delay = self.backoff_s
                        yield message
                    if not session.last_note:
                        session.on_close(NORMAL_CLOSE_CODE)
                    action = session.next_action
                    note = session.last_note
            except GatewayFatalError as exc:
                self._alert("gateway_fatal", str(exc))
                raise
            except Exception as exc:               # noqa: BLE001 - transport boundary
                prior_note = session.last_note
                action = session.on_close(self._close_code(exc))
                note = prior_note or session.last_note
                note = f"{note}: {exc}" if str(exc) else note
            if action is CloseAction.FATAL:
                self._alert("gateway_fatal", note)
                raise GatewayFatalError(note)
            self._alert("gateway_reconnect", f"{action.value}: {note}")
            await asyncio.sleep(delay)
            delay = min(self.max_backoff_s, delay * 2)

    async def _session(self, ws: Any, session: GatewaySession,
                       allowed: set[str]) -> AsyncIterator[RawMessage]:
        """One socket: HELLO -> IDENTIFY/RESUME -> DISPATCH, with a heartbeat task."""
        heartbeat: asyncio.Task[None] | None = None
        try:
            async for frame in ws:
                event = json.loads(frame)
                op = event.get("op")
                if op == 10:                       # HELLO
                    payload = event.get("d")
                    hello = payload if isinstance(payload, dict) else {}
                    await ws.send(json.dumps(session.on_hello(hello)))
                    heartbeat = asyncio.create_task(self._heartbeat(ws, session))
                    continue
                if op == 11:                       # HEARTBEAT ACK
                    session.on_heartbeat_ack()
                    continue
                if op == 1:                        # server asks for a beat now
                    await ws.send(json.dumps(session.heartbeat_frame()))
                    continue
                if op == 7:                        # RECONNECT
                    session.on_reconnect()
                    await ws.close(code=ZOMBIE_CLOSE_CODE)
                    return
                if op == 9:                        # INVALID_SESSION
                    session.on_invalid_session(bool(event.get("d")))
                    await ws.close(code=ZOMBIE_CLOSE_CODE)
                    return
                if op != 0:
                    continue
                dispatch = session.on_dispatch(event)
                if dispatch.gap:
                    self._alert(
                        "gateway_gap",
                        f"{dispatch.event_type} seq={dispatch.sequence}: "
                        f"{dispatch.reason}",
                    )
                if dispatch.duplicate:
                    continue
                if dispatch.event_type in {"READY", "RESUMED"}:
                    continue
                if dispatch.event_type != "MESSAGE_CREATE":
                    continue
                payload = dispatch.payload
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
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    async def _heartbeat(self, ws: Any, session: GatewaySession) -> None:
        """Beat with the last sequence; a missing ACK means the socket is a zombie."""
        while True:
            interval = session.heartbeat_interval_s
            await asyncio.sleep(max(1.0, interval * (1 - self.heartbeat_margin)))
            if session.pending_ack:
                session.on_zombie()
                with suppress(Exception):
                    await ws.close(code=ZOMBIE_CLOSE_CODE)
                return
            try:
                await ws.send(json.dumps(session.heartbeat_frame()))
            except Exception:  # noqa: BLE001 - the reader owns the failure path
                return

    def _alert(self, reason: str, note: str) -> None:
        """A transport alert is best-effort: a broken pager must not stop the stream."""
        if self.alert is None:
            return
        try:
            self.alert(reason, note)
        except Exception:  # noqa: BLE001 - alerting is not the trade path
            return

    @staticmethod
    def _close_code(exc: BaseException) -> int:
        """Extract a Discord close code from a websockets exception, or 0."""
        code = getattr(exc, "code", None)
        if isinstance(code, int):
            return code
        received = getattr(exc, "rcvd", None)
        code = getattr(received, "code", None)
        return code if isinstance(code, int) else 0

    def _name_for(self, channel_id: str) -> str:
        for name, cid in self.name_to_id.items():
            if cid == channel_id:
                return name
        return channel_id

"""One supervised asyncio paper session: Discord post -> decision -> paper order intent.

Hot path (``on_post`` / ``on_entry_due``) is pure in-memory work on the event loop: existing parser,
frozen forward rules, an O(1) per-symbol quote read, a Decimal limit, and an intent-only recorder.
All disk I/O (journal fsync, latch persistence) is handed to worker threads and never awaited by the
decision. The latch is mirrored in memory and re-read from disk at 1 Hz; a trip is applied to the
mirror before it is persisted, so a trip can never be slower than its own write.

Paper only. The only sinks are PaperIntentBroker and ReviewOnlyBroker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import queue
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path

from .alerts import Page, Pager
from .broker import ExecutionMode, ExecutionResult, PaperIntentBroker, ReviewOnlyBroker
from .config import (
    ALLOWED_CHANNEL_IDS,
    CHANNELS,
    EXECUTION_COMPONENT,
    EXECUTION_LIMITS,
    MARKET_TZ,
    STALE_POLICY,
    ExecutionLimits,
    ForwardRules,
    StalePolicy,
)
from .domain import Effect, EffectKind, EventKind, RawDiscordMessage
from .execution_model import CallSpec, EntrySchedule, Track, entry_schedule, track_for
from .fail_safe_control import NoTradeSafetyLatch, SafetyMode
from .fastpath import QuoteCache
from .health import FeedHealthMonitor
from .parser import parse_message
from .pricing import guarded_entry_limit, reference_price

LOGGER = logging.getLogger(__name__)
_OK_CHANNEL_ID = CHANNELS["options-king"]


# --------------------------------------------------------------------------- journal (off-path)


class JournalError(RuntimeError):
    """The paper evidence stream cannot be trusted to persist."""


class Journal:
    """Bounded asynchronous writer. Refuse backlog, writer failure and silent restart.

    Existing files are never appended to: the session cannot yet restore working calls.
    Successful close proves that the writer drained its queue and fsync'd the records.
    This is paper-only; real external effects require a durable admission first.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        on_durable: Callable[[int, int], None] | None = None,
        batch: int = 64,
        max_pending: int = 8192,
    ) -> None:
        if batch <= 0 or max_pending <= 0:
            raise ValueError("journal batch and queue bound must be positive")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=max_pending)
        self._on_durable = on_durable
        self._batch = batch
        self._seq = 0
        self._closed = False
        self._failure: str | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="exec-journal", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            raise JournalError("journal_start_timeout")
        self.check()

    def check(self) -> None:
        if self._failure is not None:
            raise JournalError(self._failure)

    def submit(self, record: dict[str, object]) -> int:
        with self._lock:
            self.check()
            if self._closed:
                raise JournalError("journal_closed")
            next_seq = self._seq + 1
            row = {**record, "seq": next_seq}
            try:
                self._queue.put_nowait(row)
            except queue.Full as exc:
                raise JournalError("journal_backpressure") from exc
            self._seq = next_seq
            return next_seq

    def _run(self) -> None:
        try:
            with self.path.open("x", encoding="utf-8") as handle:
                self._ready.set()
                while True:
                    pending = [self._queue.get()]
                    while len(pending) < self._batch:
                        try:
                            pending.append(self._queue.get_nowait())
                        except queue.Empty:
                            break
                    stop = False
                    written: list[int] = []
                    for record in pending:
                        if record is None:
                            stop = True
                            continue
                        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
                        written.append(int(str(record["seq"])))
                    handle.flush()
                    os.fsync(handle.fileno())
                    if self._on_durable is not None:
                        durable_ns = time.perf_counter_ns()
                        for seq in written:
                            self._on_durable(seq, durable_ns)
                    if stop:
                        return
        except Exception as exc:
            self._failure = f"journal_writer:{type(exc).__name__}"
        finally:
            self._ready.set()

    def close(self, timeout_s: float = 5.0) -> None:
        if timeout_s <= 0:
            raise ValueError("journal close timeout must be positive")
        with self._lock:
            self.check()
            if self._closed:
                if self._thread.is_alive():
                    raise JournalError("journal_close_incomplete")
                return
            self._closed = True
        deadline = time.monotonic() + timeout_s
        try:
            self._queue.put(None, timeout=timeout_s)
        except queue.Full as exc:
            raise JournalError("journal_close_backpressure") from exc
        self._thread.join(max(0.0, deadline - time.monotonic()))
        self.check()
        if self._thread.is_alive():
            raise JournalError("journal_close_timeout")


# ------------------------------------------------------------------------------ latch mirror


class LatchMirror:
    """In-memory view of :class:`NoTradeSafetyLatch` for the hot path; disk stays authoritative.

    * Starts from disk (an uninitialized latch is NO_TRADE: fail closed).
    * ``trip`` flips the mirror first, then persists on a worker thread.
    * Only ``scorpion latch clear --operator NAME`` (a person) arms it; the session never clears.
    * The fsync'd emergency sentinel is stat'ed on every check so a trip from another process is
      honoured immediately, not at the next 1 Hz refresh.
    """

    def __init__(self, latch: NoTradeSafetyLatch, component: str = EXECUTION_COMPONENT) -> None:
        self.latch = latch
        self.component = component
        self._armed = latch.state(component).mode is SafetyMode.NORMAL
        self._sentinel_check = _sentinel_checker(latch, component)

    @property
    def armed(self) -> bool:
        return self._armed and not self._sentinel_check()

    def refresh(self) -> bool:
        self._armed = self.latch.state(self.component).mode is SafetyMode.NORMAL
        return self._armed

    def trip(self, reason: str) -> bool:
        """Returns True if this call changed ARMED -> NO_TRADE (used for page dedup)."""

        was_armed = self._armed
        self._armed = False
        self.latch.trip_no_trade(self.component, reason=reason)
        return was_armed


def _sentinel_checker(latch: NoTradeSafetyLatch, component: str) -> Callable[[], bool]:
    return lambda: latch.emergency_sentinel_active(component)


# --------------------------------------------------------------------------------- decisions


@dataclass(slots=True)
class WorkingCall:
    spec: CallSpec
    schedule: EntrySchedule
    track: Track
    post_recv_ns: int
    limit: Decimal | None = None
    chase_limit: Decimal | None = None
    status: str = "scheduled"
    intended_qty: int = 0


@dataclass(frozen=True, slots=True)
class Decision:
    call_id: str
    message_id: str
    ticker: str | None
    action: str
    track: str
    latency_ns: int
    entry_ts_utc: datetime | None = None


@dataclass
class LatencyRecorder:
    samples_ns: dict[str, list[int]] = field(default_factory=dict)

    def add(self, name: str, value_ns: int) -> None:
        self.samples_ns.setdefault(name, []).append(value_ns)

    def summary_ms(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for name, values in self.samples_ns.items():
            ordered = sorted(values)
            out[name] = {
                "n": float(len(ordered)),
                "p50_ms": percentile(ordered, 50) / 1e6,
                "p99_ms": percentile(ordered, 99) / 1e6,
                "max_ms": ordered[-1] / 1e6 if ordered else 0.0,
            }
        return out


def percentile(ordered: list[int], pct: float) -> float:
    if not ordered:
        return 0.0
    rank = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return float(ordered[rank])


class PaperExecutionSession:
    def __init__(
        self,
        *,
        rules: ForwardRules,
        cache: QuoteCache,
        latch: LatchMirror,
        pager: Pager,
        journal: Journal | None = None,
        stale: StalePolicy = STALE_POLICY,
        limits: ExecutionLimits = EXECUTION_LIMITS,
        mode: ExecutionMode = ExecutionMode.PAPER,
        allowed_author_ids: frozenset[str] | None = None,
        mono_ns: Callable[[], int] = time.monotonic_ns,
        now_utc: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if mode not in (ExecutionMode.PAPER, ExecutionMode.REVIEW_ONLY):
            raise ValueError("paper session supports PAPER or REVIEW_ONLY only")
        self.rules = rules
        self.cache = cache
        self.latch = latch
        self.pager = pager
        self.journal = journal
        self.stale = stale
        self.limits = limits
        self.mode = mode
        self.allowed_author_ids = allowed_author_ids
        self._mono_ns = mono_ns
        self._now_utc = now_utc
        self._broker = PaperIntentBroker() if mode is ExecutionMode.PAPER else ReviewOnlyBroker()
        self.working: dict[str, WorkingCall] = {}
        self.orders: list[ExecutionResult] = []
        self.latency = LatencyRecorder()

    # -- hot path 1: a Discord post arrives --------------------------------------------------
    def on_post(self, raw: RawDiscordMessage, *, recv_ns: int | None = None) -> Decision:
        started = time.perf_counter_ns() if recv_ns is None else recv_ns
        decision = self._decide(raw, started)
        self.latency.add("post_to_decision", decision.latency_ns)
        self._journal({"kind": "decision", **_decision_record(decision)})
        return decision

    def _decide(self, raw: RawDiscordMessage, started: int) -> Decision:
        def done(
            action: str,
            *,
            ticker: str | None = None,
            track: str = "",
            call_id: str = "",
            entry: datetime | None = None,
        ) -> Decision:
            return Decision(
                call_id,
                raw.message_id,
                ticker,
                action,
                track,
                time.perf_counter_ns() - started,
                entry,
            )

        if raw.channel_id not in ALLOWED_CHANNEL_IDS or raw.channel_id != _OK_CHANNEL_ID:
            return done("ignore:channel")
        if raw.edited_ts_utc is not None:
            return done("ignore:edit_revision")
        event = parse_message(raw, self.allowed_author_ids)
        if event.kind is not EventKind.ENTRY or event.ticker is None:
            return done(f"ignore:{event.kind.value.lower()}")
        ticker = event.ticker.upper()
        if event.option_side != "CALL":
            return done("skip:not_call", ticker=ticker)
        if ticker in self.rules.excluded_tickers:
            return done("skip:excluded_ticker", ticker=ticker)
        post_et = raw.source_ts_utc.astimezone(MARKET_TZ)
        seen_et = raw.received_ts_utc.astimezone(MARKET_TZ)
        spec = CallSpec(
            date=post_et.strftime("%Y-%m-%d"),
            time_et=post_et.strftime("%H:%M"),
            ticker=ticker,
            side="call",
            strike=str(event.strike or ""),
            expiry=event.expiry.isoformat() if event.expiry else "",
            seen_time_et=seen_et.strftime("%H:%M"),
        )
        schedule = entry_schedule(raw.source_ts_utc, raw.received_ts_utc, self.rules)
        track = track_for(raw.source_ts_utc, self.rules)
        if schedule.late:
            return done("skip:late_gt15min", ticker=ticker, track=track.value, call_id=spec.call_id)
        if spec.call_id in self.working:
            return done("ignore:duplicate_call", ticker=ticker, call_id=spec.call_id)
        self.working[spec.call_id] = WorkingCall(spec, schedule, track, started)
        action = "schedule_entry" if track is Track.MAIN else "shadow_log_only"
        return done(
            action, ticker=ticker, track=track.value, call_id=spec.call_id, entry=schedule.entry_ts
        )

    # -- hot path 2: the entry minute arrives --------------------------------------------------
    def on_entry_due(self, call_id: str, *, chase: bool = False) -> ExecutionResult | None:
        started = time.perf_counter_ns()
        working = self.working.get(call_id)
        if working is None or working.track is not Track.MAIN:
            return None
        if working.status != ("limit_intent" if chase else "scheduled"):
            return None
        if not self.latch.armed:
            working.status = "blocked:no_trade_latch"
            self._journal({"kind": "skip", "call_id": call_id, "reason": working.status})
            return None
        quote = self.cache.fresh(
            working.spec.ticker, max_age_ms=self.stale.quote_max_age_ms, now_mono_ns=self._mono_ns()
        )
        anchor = working.limit if chase and working.limit is not None else reference_price(quote)
        limit = (
            guarded_entry_limit(quote, anchor, max_chase_bps=self.limits.max_chase_bps)
            if anchor is not None
            else None
        )
        if limit is None:
            working.status = "skip:stale_or_missing_quote"
            self._journal(
                {"kind": "skip", "call_id": call_id, "reason": working.status, "quote": repr(quote)}
            )
            self.latency.add("entry_to_intent", time.perf_counter_ns() - started)
            return None
        if not chase:
            # First limit rests at the current price (R1); it is not lifted to the ask.
            limit = min(limit, anchor) if anchor is not None else limit
        quantity = int(self.rules.paper_notional_usd // limit)
        if quantity <= 0:
            working.status = "skip:below_one_share_budget"
            self._journal({"kind": "skip", "call_id": call_id, "reason": working.status})
            return None
        effect = Effect(
            EffectKind.PROPOSE_OPEN,
            working.spec.ticker,
            call_id,
            1,
            "fwd-v2 chase" if chase else "fwd-v2 entry",
            quantity_hint=quantity,
        )
        result = self._broker.execute(effect, quantity, limit)
        if chase:
            working.chase_limit = limit
            working.status = "chase_intent"
        else:
            working.limit = limit
            working.status = "limit_intent"
        working.intended_qty = quantity
        self.orders.append(result)
        elapsed = time.perf_counter_ns() - started
        self.latency.add("entry_to_intent", elapsed)
        self._journal(
            {
                "kind": "intent",
                "call_id": call_id,
                "ticker": working.spec.ticker,
                "chase": chase,
                "conditional_on": "initial_unfilled" if chase else None,
                "limit": str(limit),
                "quantity": quantity,
                "status": result.status,
                "mode": result.mode.value,
                "latency_us": elapsed // 1_000,
            }
        )
        return result

    def on_exit_due(self, call_id: str) -> ExecutionResult | None:
        working = self.working.get(call_id)
        if (
            working is None or working.limit is None or working.intended_qty <= 0
            or working.status == "exit_intent"
        ):
            return None
        effect = Effect(EffectKind.PROPOSE_CLOSE, working.spec.ticker, call_id, 1, "fwd-v2 60m exit")
        result = self._broker.execute(effect, working.intended_qty, None)
        working.status = "exit_intent"
        self.orders.append(result)
        self._journal({
            "kind": "conditional_exit_intent", "call_id": call_id,
            "status": result.status, "conditional_on": "confirmed_entry_fill",
            "quantity": working.intended_qty,
        })
        return result

    # -- health -------------------------------------------------------------------------------
    def health_tick(self, monitor: FeedHealthMonitor, *, session_open: bool) -> list[str]:
        if self.journal is not None:
            self.journal.check()
        watched = [w.spec.ticker for w in self.working.values() if w.track is Track.MAIN]
        health = monitor.evaluate(
            now_mono_ns=self._mono_ns(), session_open=session_open, watched=watched
        )
        for warning in health.warnings:
            LOGGER.warning("feed health: %s", warning)
        if health.latch_reasons:
            reason = ";".join(health.latch_reasons)
            self.latch.trip(reason)
            kind = health.latch_reasons[0].split("=", 1)[0]
            day = self._now_utc().astimezone(MARKET_TZ).date().isoformat()
            self.pager.page(Page(f"scorpion-paper:{kind}:{day}", f"NO_TRADE latched: {reason}"))
            self._journal({"kind": "latch", "reason": reason})
        return list(health.latch_reasons)

    def _journal(self, record: dict[str, object]) -> None:
        if self.journal is not None:
            record.setdefault("ts_utc", self._now_utc().isoformat())
            self.journal.submit(record)


def _decision_record(decision: Decision) -> dict[str, object]:
    record = asdict(decision)
    record["latency_us"] = decision.latency_ns // 1_000
    return record


# ------------------------------------------------------------------------------- supervision


def sd_notify(message: str) -> bool:
    """Minimal systemd notify (READY=1 / WATCHDOG=1) without a dependency."""

    target = os.environ.get("NOTIFY_SOCKET")
    if not target:
        return False
    address = "\0" + target[1:] if target.startswith("@") else target
    with contextlib.closing(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)) as sock:
        sock.connect(address)
        sock.sendall(message.encode())
    return True


async def schedule_at(
    when_utc: datetime, now_utc: Callable[[], datetime], action: Callable[[], object]
) -> None:
    delay = (when_utc - now_utc()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    action()


async def supervise(
    session: PaperExecutionSession,
    *,
    monitor: FeedHealthMonitor,
    feed_run: Callable[[], object] | None,
    ingest_queue: asyncio.Queue[tuple[RawDiscordMessage, int]],
    on_new_symbol: Callable[[str], None] | None = None,
    session_open: Callable[[], bool],
    stop_at_utc: datetime,
    tick_s: float = 1.0,
    ingest_run: Callable[[], object] | None = None,
) -> None:
    """Run feed, ingress, consumer, health (1 Hz) and pager in one TaskGroup.

    Any task crash latches NO_TRADE, pages once and re-raises so systemd restarts the unit; the
    restart comes back NO_TRADE because only a person clears the latch.
    """

    now_utc = session._now_utc

    async def consume() -> None:
        while True:
            raw, recv_ns = await ingest_queue.get()
            decision = session.on_post(raw, recv_ns=recv_ns)
            if decision.action == "schedule_entry" and on_new_symbol and decision.ticker:
                on_new_symbol(decision.ticker)
            if decision.entry_ts_utc is not None and decision.action == "schedule_entry":
                working = session.working[decision.call_id]
                call_id = decision.call_id
                tg.create_task(
                    schedule_at(
                        working.schedule.entry_ts, now_utc, partial(session.on_entry_due, call_id)
                    )
                )
                tg.create_task(
                    schedule_at(
                        working.schedule.chase_ts,
                        now_utc,
                        partial(session.on_entry_due, call_id, chase=True),
                    )
                )
                tg.create_task(
                    schedule_at(
                        working.schedule.exit_ts, now_utc, partial(session.on_exit_due, call_id)
                    )
                )

    async def health() -> None:
        while True:
            await asyncio.to_thread(session.latch.refresh)
            session.health_tick(monitor, session_open=session_open())
            sd_notify("WATCHDOG=1")
            if now_utc() >= stop_at_utc:
                raise _SessionComplete
            await asyncio.sleep(tick_s)

    async def required_component(factory: Callable[[], object], name: str) -> None:
        await _as_coroutine(factory)
        raise RuntimeError(f"required_component_ended:{name}")

    try:
        async with asyncio.TaskGroup() as tg:
            if feed_run is not None:
                tg.create_task(required_component(feed_run, "feed"), name="databento-feed")
            if ingest_run is not None:
                tg.create_task(required_component(ingest_run, "ingress"), name="discord-ingest")
            tg.create_task(consume(), name="call-ingest-decision")
            tg.create_task(health(), name="health-1hz")
            tg.create_task(session.pager.run(), name="pager")
            sd_notify("READY=1")
    except* _SessionComplete:
        LOGGER.info("session complete at %s", stop_at_utc.isoformat())
    except* Exception as group:
        reason = "task_crash:" + ",".join(type(e).__name__ for e in group.exceptions)
        session.latch.trip(reason)
        session.pager.page(Page(f"scorpion-paper:crash:{now_utc().date()}", reason))
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.pager.drain_once(), 5.0)
        raise


class _SessionComplete(Exception):
    pass


async def _as_coroutine(factory: Callable[[], object]) -> None:
    result = factory()
    if asyncio.iscoroutine(result):
        await result


def stop_time_utc(day_close_et: datetime) -> datetime:
    return (day_close_et + timedelta(minutes=5)).astimezone(UTC)

"""Bounded, non-blocking runtime for the hot path's quote and order I/O.

Why this module exists
----------------------
``Listener.handle`` runs the whole decision on the caller's thread, and the two
pieces of it that can hang both do their work synchronously: ``broker.place``
retries up to three times with a five-second HTTP timeout, and the quote feed is
a blocking ``urllib`` call. On 9/8 the failure was not a bad trade, it was a
silence: one slow dependency and the Gateway heartbeat, every later alert, and
the expiry clock all wait behind it.

This module is the bounded seam for that work. It is deliberately *not* a
general-purpose async framework; it is four small machines whose failure modes
are named:

  * ``BoundedRuntime`` - a fixed worker pool with a **bounded** submission queue.
    A full queue is refused as ``queue_full``; it never grows and it never
    silently drops work. Every submission carries a deadline, and a stage that
    overruns it is reported as ``deadline_exceeded`` **without** cancelling the
    underlying work (a stage that is still running may still complete; a waiter
    that gave up is a measurement, not a kill).
  * ``StageTimer`` - per-stage (quote/decision/order) latency recorded against
    the existing ``health.LatencyBudget`` semantics, so ``health`` and ``drift``
    can read one snapshot instead of inventing a second clock.
  * ``QuotePipeline`` - a single-flight quote cache. Concurrent requests for the
    same contract share one fetch; a slow fetch for one contract never blocks a
    different contract, because each contract has its own flight.
  * ``RuntimeQuoteProvider`` / ``RuntimeBroker`` and the async ``run_in_worker``
    bridge - the two small adapters the ``Listener`` can opt into. With the
    runtime disabled (the default) every one of them is an exact passthrough, so
    the live path is byte-identical until a human turns the runtime on.

Design rules (they matter on a desk that places real orders)
-----------------------------------------------------------
  * **Default off.** ``BoundedRuntime()`` is disabled. ``submit``/``call`` run
    the callable inline and spawn no threads. Turning it on is explicit:
    ``BoundedRuntime(enabled=True, ...)`` or ``BoundedRuntime.from_env()`` with
    ``MFF_RUNTIME=1``.
  * **Named refusals, never silent skips.** ``queue_full``, ``deadline_exceeded``,
    ``runtime_closed``, ``worker_failed``, ``cancelled`` and ``no_quote`` are
    string constants so a caller can write them to the fill log verbatim.
  * **Deadlines are non-terminal.** A deadline is observed by the waiter; the
    worker keeps running. Cancelling a stage would turn "we stopped waiting" into
    "the venue was never asked", which is not the same fact and is the wrong one
    to believe about an order.
  * **No wall clock in the logic.** All timing comes from an injected ``clock``
    (``time.monotonic`` by default); tests pass a fake clock and get exact
    numbers.
  * **Stdlib only.** ``concurrent.futures`` and ``asyncio`` are the whole
    concurrency toolkit here.

The one wiring rule the Lead must keep: a runtime that is wired but disabled
changes nothing. See ``RuntimeQuoteProvider`` and ``RuntimeBroker``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from .broker import Ack, BrokerError, QuoteProvider, TransportError
from .health import LatencyBudget
from .types import OrderIntent, Quote

# ── named reasons ────────────────────────────────────────────────────────────
# Every refusal is one of these strings. The fill log carries them verbatim, so
# the reason a stage did nothing is legible after the fact.
QUEUE_FULL = "queue_full"
DEADLINE_EXCEEDED = "deadline_exceeded"
RUNTIME_CLOSED = "runtime_closed"
WORKER_FAILED = "worker_failed"
CANCELLED = "cancelled"
NO_QUOTE = "no_quote"
NO_CONTRACT = "no_contract"

# ── stage names ──────────────────────────────────────────────────────────────
STAGE_QUOTE = "quote"
STAGE_DECISION = "decision"
STAGE_ORDER = "order"
STAGES: tuple[str, ...] = (STAGE_QUOTE, STAGE_DECISION, STAGE_ORDER)

# ── quote sources (why a quote call cost what it did) ────────────────────────
SOURCE_CACHE = "cache"
SOURCE_FETCH = "fetch"
SOURCE_COALESCED = "coalesced"
SOURCE_REFUSED = "refused"

# ── defaults ─────────────────────────────────────────────────────────────────
DEFAULT_WORKERS = 2
DEFAULT_QUEUE_CAPACITY = 8
DEFAULT_DEADLINE_MS = 5_000
DEFAULT_QUOTE_DEADLINE_MS = 1_500
DEFAULT_PLACE_DEADLINE_MS = 8_000
DEFAULT_QUOTE_MAX_AGE_S = 5.0
DEFAULT_STALE_FLIGHT_S = 30.0


def _elapsed_ms(started: float, ended: float) -> int:
    """Whole milliseconds between two clock readings, clamped at zero.

    A coarse or NTP-stepped clock must never produce a negative latency: a
    -4000ms sample would poison every mean/percentile computed from it.
    """
    return max(0, int(round((ended - started) * 1000)))


def _env_flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(source: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


# ── exceptions ───────────────────────────────────────────────────────────────
class RuntimeFault(RuntimeError):
    """A bounded-runtime refusal. ``reason`` is always one of the named strings."""

    reason: str = WORKER_FAILED

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(f"{self.reason}: {detail}" if detail else self.reason)


class QueueFull(RuntimeFault):
    """The bounded submission queue was full; the callable never ran."""

    reason = QUEUE_FULL


class RuntimeClosed(RuntimeFault):
    """The runtime has been shut down; the callable never ran."""

    reason = RUNTIME_CLOSED


class StageDeadlineExceeded(RuntimeFault):
    """The waiter's deadline expired. The underlying stage may still be running."""

    reason = DEADLINE_EXCEEDED


class StageCancelled(RuntimeFault):
    """The stage was cancelled before a worker picked it up."""

    reason = CANCELLED


def _exception_for_reason(reason: str, detail: str = "") -> RuntimeFault:
    """Map a named reason back to the exception a caller should see."""
    if reason == QUEUE_FULL:
        return QueueFull(detail)
    if reason == RUNTIME_CLOSED:
        return RuntimeClosed(detail)
    if reason == DEADLINE_EXCEEDED:
        return StageDeadlineExceeded(detail)
    if reason == CANCELLED:
        return StageCancelled(detail)
    return RuntimeFault(detail)


# ── the stage timer ──────────────────────────────────────────────────────────
class StageTimer:
    """Per-stage latency against the existing ``LatencyBudget`` semantics.

    One total budget is kept for the end-to-end stage, plus one ``LatencyBudget``
    per named stage. That mirrors ``health.LatencyBudget`` exactly (samples,
    breaches, worst, percentile, ``summary()``) so a report can print the same
    numbers ``replay`` already prints, and a breach can page through the same
    callback hook instead of a second observability path.

    The clock is injectable. ``record()`` takes an explicit latency and is pure;
    ``start``/``stop``/``span`` read the injected clock.
    """

    def __init__(
        self,
        *,
        budget: LatencyBudget | None = None,
        stage_budget_ms: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_breach: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.budget = LatencyBudget() if budget is None else budget
        self.stage_budget_ms = self.budget.budget_ms if stage_budget_ms is None else stage_budget_ms
        self.clock = clock
        self.on_breach = on_breach
        self._lock = threading.Lock()
        self._stages: dict[str, LatencyBudget] = {
            stage: LatencyBudget(budget_ms=self.stage_budget_ms) for stage in STAGES
        }

    def budget_for(self, stage: str) -> LatencyBudget:
        """The budget for a stage, created on first use (custom stages allowed)."""
        with self._lock:
            return self._stage_budget_locked(stage)

    def record(self, stage: str, latency_ms: int) -> bool:
        """Record one sample. Returns True when it is inside the stage budget."""
        sample = max(0, int(latency_ms))
        with self._lock:
            stage_budget = self._stage_budget_locked(stage)
            within = stage_budget.record(sample)
            self.budget.record(sample)
            budget_ms = stage_budget.budget_ms
        if not within and self.on_breach is not None:
            # A broken logger must not stop trading, so the hook is best-effort.
            with suppress(Exception):
                self.on_breach(stage, sample, budget_ms)
        return within

    def start(self, stage: str) -> float:
        """Open a span; returns a token for ``stop``."""
        return self.clock()

    def stop(self, stage: str, token: float) -> int:
        """Close a span and record it. Returns the measured latency in ms."""
        latency_ms = _elapsed_ms(token, self.clock())
        self.record(stage, latency_ms)
        return latency_ms

    @contextmanager
    def span(self, stage: str) -> Iterator[None]:
        """Measure a ``with`` block: ``with timer.span(STAGE_ORDER): ...``."""
        token = self.start(stage)
        try:
            yield
        finally:
            self.stop(stage, token)

    @property
    def breaches(self) -> int:
        """Total end-to-end breach count (the number ``health`` pages on)."""
        with self._lock:
            return self.budget.breaches

    def worst_stage(self) -> str:
        """The stage with the largest worst-case sample; ``""`` when empty."""
        with self._lock:
            candidates = [
                (budget.worst_ms, stage) for stage, budget in self._stages.items() if budget.samples
            ]
        if not candidates:
            return ""
        return max(candidates)[1]

    def snapshot(self) -> dict[str, object]:
        """A JSON-ready ``{"total": ..., "stages": ..., "worst_stage": ...}`` view."""
        with self._lock:
            stages = {stage: budget.summary() for stage, budget in self._stages.items()}
            total = self.budget.summary()
        return {"total": total, "stages": stages, "worst_stage": self.worst_stage()}

    def reset(self) -> None:
        """Clear every sample and breach, keeping the budgets' ceilings."""
        with self._lock:
            self.budget.samples.clear()
            self.budget.breaches = 0
            for budget in self._stages.values():
                budget.samples.clear()
                budget.breaches = 0

    def _stage_budget_locked(self, stage: str) -> LatencyBudget:
        budget = self._stages.get(stage)
        if budget is None:
            budget = LatencyBudget(budget_ms=self.stage_budget_ms)
            self._stages[stage] = budget
        return budget


# ── results and future-like handles ──────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class StageResult[T]:
    """One stage's outcome, named rather than thrown.

    ``ok`` with a successful value is normal completion. A refusal carries one of
    the named reasons; a raised failure also carries the original exception in
    ``error`` so a caller can map a broker reject correctly instead of flattening
    every exception into "runtime broke".
    """

    ok: bool
    reason: str = ""
    label: str = ""
    stage: str = ""
    latency_ms: int = 0
    value: T | None = None
    error: BaseException | None = None


class StageState(StrEnum):
    """Handle state. ``DEADLINE_EXCEEDED`` means *observed*, not *killed*."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    REFUSED = "refused"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"


class _Job:
    """The shared state of one submission (worker side and handle side)."""

    __slots__ = (
        "deadline_ms",
        "fn",
        "future",
        "label",
        "refused_reason",
        "stage",
        "started",
        "started_at",
        "submitted_at",
    )

    def __init__(
        self,
        *,
        fn: Callable[[], Any],
        future: concurrent.futures.Future[Any],
        label: str,
        stage: str,
        deadline_ms: int,
        submitted_at: float,
        refused_reason: str = "",
    ) -> None:
        self.fn = fn
        self.future = future
        self.label = label
        self.stage = stage
        self.deadline_ms = deadline_ms
        self.submitted_at = submitted_at
        self.started = False
        self.started_at = 0.0
        self.refused_reason = refused_reason


class RuntimeFuture[T]:
    """A future-like handle for one submitted stage.

    It wraps ``concurrent.futures.Future`` so the asyncio bridge can wrap it
    directly, and adds the runtime's own vocabulary: a named refusal reason, a
    deadline measured from the *submission* instant, and the stage's latency.

    ``outcome()`` never raises for a refusal or a deadline. ``result()`` re-raises
    the stage's own exception when the stage failed (so a ``BrokerError`` stays a
    ``BrokerError``), and a ``RuntimeFault`` for a refusal.
    """

    __slots__ = ("_clock", "_deadline_hit", "_job", "_lock", "_on_deadline")

    def __init__(
        self, job: _Job, clock: Callable[[], float], on_deadline: Callable[[], None] | None = None
    ) -> None:
        self._job = job
        self._clock = clock
        self._on_deadline = on_deadline
        self._deadline_hit = False
        self._lock = threading.Lock()

    # ── introspection ────────────────────────────────────────────────────────
    @property
    def label(self) -> str:
        return self._job.label

    @property
    def stage(self) -> str:
        return self._job.stage

    @property
    def deadline_ms(self) -> int:
        return self._job.deadline_ms

    @property
    def submitted_at(self) -> float:
        return self._job.submitted_at

    @property
    def future(self) -> concurrent.futures.Future[Any]:
        """The underlying future (the asyncio bridge wraps this)."""
        return self._job.future

    @property
    def refused(self) -> bool:
        return bool(self._job.refused_reason)

    @property
    def deadline_exceeded(self) -> bool:
        """True once a waiter has observed this stage overrun its deadline."""
        return self._deadline_hit

    def elapsed_ms(self) -> int:
        return _elapsed_ms(self._job.submitted_at, self._clock())

    def done(self) -> bool:
        return self._job.future.done()

    def cancel(self) -> bool:
        """Cancel a stage that has not started. A running stage keeps running."""
        return self._job.future.cancel()

    def add_done_callback(self, callback: Callable[[RuntimeFuture[T]], None]) -> None:
        """Run ``callback`` once the stage reaches a terminal state."""
        self._job.future.add_done_callback(lambda _future: callback(self))

    @property
    def state(self) -> StageState:
        if self.refused:
            return StageState.REFUSED
        future = self._job.future
        if future.cancelled():
            return StageState.CANCELLED
        if future.done():
            return StageState.FAILED if future.exception() is not None else StageState.DONE
        if self._deadline_hit:
            return StageState.DEADLINE_EXCEEDED
        return StageState.RUNNING if self._job.started else StageState.PENDING

    # ── waiting ──────────────────────────────────────────────────────────────
    def wait(self, timeout_ms: int | None = None) -> bool:
        """Wait for the stage to finish; returns ``done()`` (never latches a deadline)."""
        concurrent.futures.wait(
            [self._job.future], timeout=self._wait_budget_ms(timeout_ms) / 1000.0
        )
        return self._job.future.done()

    def outcome(self, timeout_ms: int | None = None) -> StageResult[T]:
        """The stage result after waiting (up to its deadline). Never raises."""
        job = self._job
        if job.refused_reason:
            return self._refused_result()
        wait_ms = self._wait_budget_ms(timeout_ms)
        if wait_ms <= 0 and not job.future.done():
            self._latch_deadline()
            return self._deadline_result()
        try:
            raw = job.future.result(timeout=wait_ms / 1000.0)
        except TimeoutError as exc:
            if job.future.done():
                # The stage itself raised TimeoutError, which is a failure, not
                # the waiter's deadline. ``done()`` is what separates the two.
                return self._failed_result(job.future.exception() or exc)
            self._latch_deadline()
            return self._deadline_result()
        except concurrent.futures.CancelledError:
            return self._cancelled_result()
        except RuntimeFault as exc:
            # A refusal raised by the runtime itself (e.g. shutdown draining a
            # queued job) keeps its own name instead of becoming worker_failed.
            return StageResult(
                ok=False,
                reason=exc.reason,
                label=self.label,
                stage=self.stage,
                latency_ms=self.elapsed_ms(),
                error=exc,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a named result
            return self._failed_result(exc)
        return self._collect(raw)

    async def outcome_async(self, timeout_ms: int | None = None) -> StageResult[T]:
        """Await the stage, off the event loop. The same non-terminal deadline rule."""
        job = self._job
        if job.refused_reason:
            return self._refused_result()
        future = job.future
        if future.done():
            return self.outcome(0)
        wait_ms = self._wait_budget_ms(timeout_ms)
        if wait_ms <= 0:
            self._latch_deadline()
            return self._deadline_result()
        wrapped = asyncio.wrap_future(future)
        # ``shield`` keeps a timeout from cancelling the worker's future: the
        # waiter stops waiting, the stage does not stop running.
        try:
            async with asyncio.timeout(wait_ms / 1000.0):
                raw = await asyncio.shield(wrapped)
        except TimeoutError:
            if future.done():
                return self.outcome(0)
            self._latch_deadline()
            return self._deadline_result()
        except concurrent.futures.CancelledError:
            return self._cancelled_result()
        except RuntimeFault as exc:
            # A refusal raised by the runtime itself (e.g. shutdown draining a
            # queued job) keeps its own name instead of becoming worker_failed.
            return StageResult(
                ok=False,
                reason=exc.reason,
                label=self.label,
                stage=self.stage,
                latency_ms=self.elapsed_ms(),
                error=exc,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a named result
            return self._failed_result(exc)
        return self._collect(raw)

    def result(self, timeout_ms: int | None = None) -> T:
        """Return the value or raise: the stage's own error, or a ``RuntimeFault``."""
        outcome = self.outcome(timeout_ms)
        if outcome.ok:
            return cast(T, outcome.value)
        if outcome.error is not None:
            raise outcome.error
        raise _exception_for_reason(outcome.reason, outcome.label)

    # ── result construction ──────────────────────────────────────────────────
    def _collect(self, raw: Any) -> StageResult[T]:
        return StageResult(
            ok=True,
            label=self.label,
            stage=self.stage,
            latency_ms=self.elapsed_ms(),
            value=cast(T, raw),
        )

    def _failed_result(self, error: BaseException) -> StageResult[T]:
        return StageResult(
            ok=False,
            reason=WORKER_FAILED,
            label=self.label,
            stage=self.stage,
            latency_ms=self.elapsed_ms(),
            error=error,
        )

    def _cancelled_result(self) -> StageResult[T]:
        return StageResult(
            ok=False,
            reason=CANCELLED,
            label=self.label,
            stage=self.stage,
            latency_ms=self.elapsed_ms(),
        )

    def _deadline_result(self) -> StageResult[T]:
        return StageResult(
            ok=False,
            reason=DEADLINE_EXCEEDED,
            label=self.label,
            stage=self.stage,
            latency_ms=self.elapsed_ms(),
        )

    def _refused_result(self) -> StageResult[T]:
        return StageResult(
            ok=False,
            reason=self._job.refused_reason,
            label=self.label,
            stage=self.stage,
            latency_ms=0,
            error=self._job.future.exception(),
        )

    def _latch_deadline(self) -> None:
        with self._lock:
            if self._deadline_hit:
                return
            self._deadline_hit = True
        if self._on_deadline is not None:
            with suppress(Exception):
                self._on_deadline()

    def _wait_budget_ms(self, timeout_ms: int | None) -> int:
        """The waiter's budget: an explicit timeout, else what is left of the deadline.

        An explicit ``timeout_ms`` is the caller's own patience and overrides the
        stage deadline - that is how a caller collects a stage that overran after
        the deadline was observed (the result is still real). The documented
        Listener hooks (``call_quote``/``call_place``) pass no explicit timeout,
        so the stage deadline is what they enforce.
        """
        if timeout_ms is not None:
            return max(0, int(timeout_ms))
        return self._job.deadline_ms - self.elapsed_ms()


# ── stats ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RuntimeStats:
    """One immutable snapshot of the runtime's counters (safe to log/serialise)."""

    enabled: bool
    workers: int
    queue_capacity: int
    queue_depth: int
    in_flight: int
    peak_queue_depth: int
    peak_in_flight: int
    threads: int
    submitted: int
    completed: int
    failed: int
    queue_full: int
    closed_refusals: int
    deadline_exceeded: int
    cancelled: int


@dataclass
class _Counters:
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    queue_full: int = 0
    closed_refusals: int = 0
    deadline_exceeded: int = 0
    cancelled: int = 0
    peak_queue_depth: int = 0
    peak_in_flight: int = 0


# ── the bounded runtime ──────────────────────────────────────────────────────
class BoundedRuntime:
    """A bounded worker pool with deadlines and named refusals.

    Disabled (the default), every submission runs inline on the caller's thread:
    no thread is created, no queue exists, and the behaviour is exactly the old
    synchronous path. Enabled, submissions are executed by a fixed number of
    daemon workers drawn from a queue that holds at most ``queue_capacity`` jobs;
    a further submission is refused with ``queue_full`` rather than queued
    without bound.

    A jobs's deadline is measured from submission. It bounds the *waiter*, not
    the work: when it expires the waiter gets ``deadline_exceeded`` and the worker
    keeps going. For an order that is deliberate - "we stopped waiting" must not
    be recorded as "the venue was never asked"; the caller decides how to
    quarantine the ambiguity.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        workers: int = DEFAULT_WORKERS,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        default_deadline_ms: int = DEFAULT_DEADLINE_MS,
        clock: Callable[[], float] = time.monotonic,
        timer: StageTimer | None = None,
        on_refusal: Callable[[str, str], None] | None = None,
        name: str = "mff-runtime",
    ) -> None:
        if workers < 1:
            raise ValueError(f"workers must be >= 1, got {workers}")
        if queue_capacity < 1:
            raise ValueError(f"queue_capacity must be >= 1, got {queue_capacity}")
        if default_deadline_ms < 1:
            raise ValueError(f"default_deadline_ms must be >= 1, got {default_deadline_ms}")
        self.enabled = enabled
        self.workers = workers
        self.queue_capacity = queue_capacity
        self.default_deadline_ms = default_deadline_ms
        self.clock = clock
        self.timer = StageTimer(clock=clock) if timer is None else timer
        self.on_refusal = on_refusal
        self.name = name
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue: deque[_Job] = deque()
        self._threads: list[threading.Thread] = []
        self._in_flight = 0
        self._closed = False
        self._counters = _Counters()

    # ── configuration ────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BoundedRuntime:
        """Build from ``MFF_RUNTIME*``. Absent/flag-off means disabled (unchanged)."""
        source = os.environ if env is None else env
        return cls(
            enabled=_env_flag(source.get("MFF_RUNTIME", "")),
            workers=_env_int(source, "MFF_RUNTIME_WORKERS", DEFAULT_WORKERS),
            queue_capacity=_env_int(source, "MFF_RUNTIME_QUEUE", DEFAULT_QUEUE_CAPACITY),
            default_deadline_ms=_env_int(source, "MFF_RUNTIME_DEADLINE_MS", DEFAULT_DEADLINE_MS),
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def stats(self) -> RuntimeStats:
        with self._lock:
            counters = self._counters
            return RuntimeStats(
                enabled=self.enabled,
                workers=self.workers,
                queue_capacity=self.queue_capacity,
                queue_depth=len(self._queue),
                in_flight=self._in_flight,
                peak_queue_depth=counters.peak_queue_depth,
                peak_in_flight=counters.peak_in_flight,
                threads=len(self._threads),
                submitted=counters.submitted,
                completed=counters.completed,
                failed=counters.failed,
                queue_full=counters.queue_full,
                closed_refusals=counters.closed_refusals,
                deadline_exceeded=counters.deadline_exceeded,
                cancelled=counters.cancelled,
            )

    # ── submission ───────────────────────────────────────────────────────────
    def submit[T](
        self,
        fn: Callable[[], T],
        deadline_ms: int | None = None,
        label: str = "",
        *,
        stage: str = STAGE_DECISION,
    ) -> RuntimeFuture[T]:
        """Submit one stage. Returns a handle; never blocks on the stage itself.

        Disabled: runs ``fn`` inline and returns a completed handle. Enabled: the
        stage is queued for a worker, or refused with ``queue_full`` /
        ``runtime_closed`` if the queue cannot take it. Refusals are returned as
        a handle whose ``reason`` names the cause, so a hot path never has to
        wrap the submission site in try/except.
        """
        deadline = self.default_deadline_ms if deadline_ms is None else int(deadline_ms)
        if deadline < 1:
            raise ValueError(f"deadline_ms must be >= 1, got {deadline}")
        if not self.enabled:
            with self._lock:
                closed = self._closed
            if closed:
                return cast(
                    RuntimeFuture[T], self._refused(fn, deadline, label, stage, RUNTIME_CLOSED)
                )
            return cast(RuntimeFuture[T], self._submit_inline(fn, deadline, label, stage))
        with self._lock:
            if self._closed:
                reason = RUNTIME_CLOSED
            elif len(self._queue) >= self.queue_capacity:
                reason = QUEUE_FULL
            else:
                reason = ""
                job = _Job(
                    fn=fn,
                    future=concurrent.futures.Future(),
                    label=label,
                    stage=stage,
                    deadline_ms=deadline,
                    submitted_at=self.clock(),
                )
                counters = self._counters
                counters.submitted += 1
                self._queue.append(job)
                counters.peak_queue_depth = max(counters.peak_queue_depth, len(self._queue))
                self._ensure_workers_locked()
                self._wake.notify()
        if reason:
            return cast(RuntimeFuture[T], self._refused(fn, deadline, label, stage, reason))
        return cast(RuntimeFuture[T], self._handle(job))

    def call[T](
        self,
        fn: Callable[[], T],
        *,
        stage: str = STAGE_DECISION,
        deadline_ms: int | None = None,
        label: str = "",
    ) -> StageResult[T]:
        """Submit and wait (bounded by ``deadline_ms``); return a named result."""
        return self.submit(fn, deadline_ms, label, stage=stage).outcome()

    def call_quote[T](
        self, fn: Callable[[], T], *, deadline_ms: int | None = None, label: str = "quote"
    ) -> StageResult[T]:
        """The quote half of the Listener hook: bounded wait, named ``no_quote``-safe."""
        return self.call(fn, stage=STAGE_QUOTE, deadline_ms=deadline_ms, label=label)

    def call_place[T](
        self, fn: Callable[[], T], *, deadline_ms: int | None = None, label: str = "order"
    ) -> StageResult[T]:
        """The order half of the Listener hook: a deadline is a named ambiguity."""
        return self.call(fn, stage=STAGE_ORDER, deadline_ms=deadline_ms, label=label)

    def submit_place[T](
        self, fn: Callable[[], T], *, deadline_ms: int | None = None, label: str = "order"
    ) -> RuntimeFuture[T]:
        """Fire an order stage without waiting (for a fully async Listener.run)."""
        return self.submit(fn, deadline_ms, label, stage=STAGE_ORDER)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def shutdown(self, wait: bool = True, timeout_s: float | None = None) -> None:
        """Refuse new work, drain queued jobs with ``runtime_closed``, stop workers."""
        with self._lock:
            if self._closed:
                threads = list(self._threads)
            else:
                self._closed = True
                while self._queue:
                    job = self._queue.popleft()
                    self._counters.closed_refusals += 1
                    with suppress(concurrent.futures.InvalidStateError):
                        job.future.set_exception(
                            RuntimeClosed(job.label or "shutdown before the job ran")
                        )
                self._wake.notify_all()
                threads = list(self._threads)
        if wait:
            remaining = timeout_s
            for thread in threads:
                thread.join(remaining)
                if remaining is not None and thread.is_alive():
                    remaining = max(0.0, remaining - 0.05)

    def close(self, wait: bool = True, timeout_s: float | None = None) -> None:
        """Alias for ``shutdown`` with the same semantics."""
        self.shutdown(wait=wait, timeout_s=timeout_s)

    def __enter__(self) -> BoundedRuntime:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.shutdown(wait=False)

    # ── inline (disabled) path ───────────────────────────────────────────────
    def _submit_inline[T](
        self, fn: Callable[[], T], deadline: int, label: str, stage: str
    ) -> RuntimeFuture[T]:
        submitted_at = self.clock()
        job = _Job(
            fn=fn,
            future=concurrent.futures.Future(),
            label=label,
            stage=stage,
            deadline_ms=deadline,
            submitted_at=submitted_at,
        )
        with self._lock:
            self._counters.submitted += 1
        started = self.clock()
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - surfaced through the handle
            job.future.set_exception(exc)
            with self._lock:
                self._counters.failed += 1
        else:
            job.future.set_result(value)
            with self._lock:
                self._counters.completed += 1
        self.timer.record(stage, _elapsed_ms(started, self.clock()))
        return self._handle(job)

    # ── worker path ──────────────────────────────────────────────────────────
    def _ensure_workers_locked(self) -> None:
        while len(self._threads) < self.workers:
            index = len(self._threads)
            thread = threading.Thread(
                target=self._worker_loop, name=f"{self.name}-{index}", daemon=True
            )
            self._threads.append(thread)
            thread.start()

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._closed:
                    self._wake.wait()
                if self._closed and not self._queue:
                    return
                job = self._queue.popleft()
                if job.future.cancelled():
                    self._counters.cancelled += 1
                    continue
                job.started = True
                job.started_at = self.clock()
                self._in_flight += 1
                counters = self._counters
                counters.peak_in_flight = max(counters.peak_in_flight, self._in_flight)
            started = self.clock()
            error: Exception | None = None
            value: Any = None
            try:
                value = job.fn()
            except Exception as exc:  # noqa: BLE001 - surfaced through the handle
                error = exc
            latency_ms = _elapsed_ms(started, self.clock())
            # Update the counters BEFORE completing the future: a waiter that
            # wakes on the future must never see stats that lag its own result.
            with self._lock:
                self._in_flight -= 1
                if error is None:
                    self._counters.completed += 1
                else:
                    self._counters.failed += 1
                self._wake.notify_all()
            if error is None:
                with suppress(concurrent.futures.InvalidStateError):
                    job.future.set_result(value)
            else:
                with suppress(concurrent.futures.InvalidStateError):
                    job.future.set_exception(error)
            # The stage timer records the worker's own execution time, not the
            # waiter's patience: a deadline exceeded while the venue still thinks
            # is a different fact from a stage that took 9 seconds to finish.
            self.timer.record(job.stage, latency_ms)

    # ── handle construction ──────────────────────────────────────────────────
    def _handle(self, job: _Job) -> RuntimeFuture[Any]:
        return RuntimeFuture(job, self.clock, self._note_deadline)

    def _refused(
        self, fn: Callable[[], Any], deadline: int, label: str, stage: str, reason: str
    ) -> RuntimeFuture[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        future.set_exception(_exception_for_reason(reason, label))
        job = _Job(
            fn=fn,
            future=future,
            label=label,
            stage=stage,
            deadline_ms=deadline,
            submitted_at=self.clock(),
            refused_reason=reason,
        )
        with self._lock:
            if reason == QUEUE_FULL:
                self._counters.queue_full += 1
            else:
                self._counters.closed_refusals += 1
        if self.on_refusal is not None:
            with suppress(Exception):
                self.on_refusal(reason, label)
        return RuntimeFuture(job, self.clock)

    def _note_deadline(self) -> None:
        with self._lock:
            self._counters.deadline_exceeded += 1


# ── the async bridge ─────────────────────────────────────────────────────────
async def run_in_worker[T](
    fn: Callable[[], T],
    *,
    timeout_ms: int | None = None,
    runtime: BoundedRuntime | None = None,
    label: str = "",
    stage: str = STAGE_DECISION,
    clock: Callable[[], float] = time.monotonic,
) -> StageResult[T]:
    """Run a blocking ``fn`` without blocking the event loop.

    With a ``runtime`` the stage goes through the bounded pool (and its queue
    refusal semantics apply). Without one, a fresh executor thread is used; that
    unbounded fallback exists for callers that have not opted into the runtime,
    not as the recommended live path.

    A timeout is reported as ``deadline_exceeded`` and does **not** kill the
    thread: the work keeps running, exactly as the ``BoundedRuntime`` contract
    requires. This is the sync-to-async half of the Listener bridge, so a slow
    venue call cannot starve the Gateway heartbeat.
    """
    if runtime is not None:
        handle = runtime.submit(fn, timeout_ms, label, stage=stage)
        return await handle.outcome_async()
    started = clock()
    loop = asyncio.get_running_loop()
    executor_future: asyncio.Future[T] = loop.run_in_executor(None, fn)
    timeout_s = None if timeout_ms is None else max(0, int(timeout_ms)) / 1000.0

    def _latency() -> int:
        return _elapsed_ms(started, clock())

    def _failed(error: BaseException) -> StageResult[T]:
        return StageResult(
            ok=False,
            reason=WORKER_FAILED,
            label=label,
            stage=stage,
            latency_ms=_latency(),
            error=error,
        )

    try:
        if timeout_s is None:
            raw = await asyncio.shield(executor_future)
        else:
            raw = await asyncio.wait_for(asyncio.shield(executor_future), timeout_s)
    except TimeoutError as exc:
        if executor_future.done():
            # The callable itself raised TimeoutError; that is a stage failure.
            stage_error: BaseException | None = None
            with suppress(concurrent.futures.CancelledError):
                stage_error = executor_future.exception()
            return _failed(stage_error or exc)
        return StageResult(
            ok=False, reason=DEADLINE_EXCEEDED, label=label, stage=stage, latency_ms=_latency()
        )
    except concurrent.futures.CancelledError:
        return StageResult(
            ok=False, reason=CANCELLED, label=label, stage=stage, latency_ms=_latency()
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a named result
        return _failed(exc)
    return StageResult(ok=True, label=label, stage=stage, latency_ms=_latency(), value=raw)


# ── single-flight quote pipeline ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class QuoteOutcome[T]:
    """One quote request: the value (or the named reason it is unusable) plus cost."""

    key: str
    ok: bool
    quote: T | None = None
    reason: str = ""
    source: str = ""
    latency_ms: int = 0
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class QuotePipelineStats:
    """Immutable counters for the single-flight pipeline."""

    fetches: int
    coalesced: int
    cache_hits: int
    no_quote: int
    errors: int
    deadline_exceeded: int
    stale_flights: int
    in_flight: int


@dataclass
class _Flight[T]:
    """One in-progress fetch shared by every caller asking for the same key."""

    key: str
    handle: RuntimeFuture[T]
    started_at: float
    waiters: int = 0


@dataclass
class _QuoteCounters:
    fetches: int = 0
    coalesced: int = 0
    cache_hits: int = 0
    no_quote: int = 0
    errors: int = 0
    deadline_exceeded: int = 0
    stale_flights: int = 0


class QuotePipeline[T]:
    """Single-flight, max-age quote cache that never serialises across contracts.

    ``fetch(contract_key)`` is the blocking feed call. Each contract gets its own
    flight, so a slow quote for ``QQQ`` does not hold a lock, a worker or a
    caller needed by ``NVDA``. Concurrent callers for the *same* contract join
    one flight instead of hammering the feed with duplicates.

    A flight older than ``stale_flight_s`` is abandoned (a wedged HTTP call must
    not become a permanent per-contract outage) and a fresh fetch is started;
    both flights are counted so the operator can see it happened.

    With no ``runtime`` argument the pipeline owns an enabled runtime; pass an
    existing one to share the pool with order I/O. Close it when done.
    """

    def __init__(
        self,
        fetch: Callable[[str], T],
        *,
        runtime: BoundedRuntime | None = None,
        max_age_s: float = DEFAULT_QUOTE_MAX_AGE_S,
        stale_flight_s: float = DEFAULT_STALE_FLIGHT_S,
        deadline_ms: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fetch = fetch
        self.max_age_s = max_age_s
        self.stale_flight_s = stale_flight_s
        self.deadline_ms = deadline_ms
        self.clock = clock
        if runtime is None:
            self.runtime = BoundedRuntime(enabled=True, clock=clock, name="mff-quotes")
            self._owns_runtime = True
        else:
            self.runtime = runtime
            self._owns_runtime = False
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, T]] = {}
        self._flights: dict[str, _Flight[T]] = {}
        self._counters = _QuoteCounters()
        self._closed = False

    # ── public ───────────────────────────────────────────────────────────────
    def quote(self, contract_key: str, *, deadline_ms: int | None = None) -> QuoteOutcome[T]:
        """Fetch (or join/cache) one contract's quote, bounded by the deadline."""
        started = self.clock()
        if not contract_key:
            return QuoteOutcome(key="", ok=False, reason=NO_CONTRACT, source=SOURCE_REFUSED)
        cached, flight, fresh = self._acquire(contract_key)
        if cached is not None:
            return cached
        assert flight is not None
        result = flight.handle.outcome(deadline_ms)
        return self._as_outcome(contract_key, result, started, fresh)

    async def aquote(self, contract_key: str, *, deadline_ms: int | None = None) -> QuoteOutcome[T]:
        """The async form: awaits the shared flight without blocking the loop."""
        started = self.clock()
        if not contract_key:
            return QuoteOutcome(key="", ok=False, reason=NO_CONTRACT, source=SOURCE_REFUSED)
        cached, flight, fresh = self._acquire(contract_key)
        if cached is not None:
            return cached
        assert flight is not None
        result = await flight.handle.outcome_async(deadline_ms)
        return self._as_outcome(contract_key, result, started, fresh)

    def invalidate(self, contract_key: str | None = None) -> None:
        """Drop one contract's cached quote, or the whole cache."""
        with self._lock:
            if contract_key is None:
                self._cache.clear()
            else:
                self._cache.pop(contract_key, None)

    def stats(self) -> QuotePipelineStats:
        with self._lock:
            counters = self._counters
            return QuotePipelineStats(
                fetches=counters.fetches,
                coalesced=counters.coalesced,
                cache_hits=counters.cache_hits,
                no_quote=counters.no_quote,
                errors=counters.errors,
                deadline_exceeded=counters.deadline_exceeded,
                stale_flights=counters.stale_flights,
                in_flight=len(self._flights),
            )

    def close(self, wait: bool = True, timeout_s: float = 5.0) -> None:
        """Stop accepting quotes and release the owned runtime when we own it."""
        with self._lock:
            self._closed = True
            self._cache.clear()
            self._flights.clear()
        if self._owns_runtime:
            self.runtime.shutdown(wait=wait, timeout_s=timeout_s)

    def __enter__(self) -> QuotePipeline[T]:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close(wait=False)

    # ── internals ────────────────────────────────────────────────────────────
    def _acquire(self, contract_key: str) -> tuple[QuoteOutcome[T] | None, _Flight[T] | None, bool]:
        """Cache hit, existing flight, or a new flight. Returns (hit, flight, fresh)."""
        now = self.clock()
        with self._lock:
            if self._closed:
                return (
                    QuoteOutcome(
                        key=contract_key, ok=False, reason=RUNTIME_CLOSED, source=SOURCE_REFUSED
                    ),
                    None,
                    False,
                )
            cached = self._cache.get(contract_key)
            if cached is not None and now - cached[0] <= self.max_age_s:
                self._counters.cache_hits += 1
                latency_ms = _elapsed_ms(now, self.clock())
                return (
                    QuoteOutcome(
                        key=contract_key,
                        ok=True,
                        quote=cached[1],
                        source=SOURCE_CACHE,
                        latency_ms=latency_ms,
                    ),
                    None,
                    False,
                )
            flight = self._flights.get(contract_key)
            if flight is not None and now - flight.started_at <= self.stale_flight_s:
                flight.waiters += 1
                self._counters.coalesced += 1
                return None, flight, False
            if flight is not None:
                self._counters.stale_flights += 1
            handle = self.runtime.submit(
                lambda: self.fetch(contract_key),
                self.deadline_ms,
                f"quote:{contract_key}",
                stage=STAGE_QUOTE,
            )
            new_flight: _Flight[T] = _Flight(key=contract_key, handle=handle, started_at=now)
            self._flights[contract_key] = new_flight
            self._counters.fetches += 1

        # Registering outside the lock matters: with a disabled runtime the
        # submission already finished, so the callback runs synchronously and
        # would deadlock on a non-reentrant pipeline lock.
        def on_done(_done: RuntimeFuture[T]) -> None:
            self._finalize(contract_key, new_flight)

        handle.add_done_callback(on_done)
        return None, new_flight, True

    def _finalize(self, contract_key: str, flight: _Flight[T]) -> None:
        """Worker-side completion: cache a good quote and retire the flight."""
        outcome = flight.handle.outcome(0)
        with self._lock:
            if self._flights.get(contract_key) is flight:
                del self._flights[contract_key]
            if outcome.ok and outcome.value is not None:
                self._cache[contract_key] = (self.clock(), outcome.value)
            elif outcome.ok:
                self._counters.no_quote += 1
            # Deadline/error/refusal observations are counted when a waiter sees
            # them in ``_as_outcome``; doing it here too would double-count.

    def _as_outcome(
        self, contract_key: str, result: StageResult[T], started: float, fresh: bool
    ) -> QuoteOutcome[T]:
        source = SOURCE_FETCH if fresh else SOURCE_COALESCED
        latency_ms = _elapsed_ms(started, self.clock())
        if result.ok and result.value is not None:
            return QuoteOutcome(
                key=contract_key, ok=True, quote=result.value, source=source, latency_ms=latency_ms
            )
        if result.ok:
            # The feed answered "there is no quote". That is a refusal to price,
            # not an error: the engine treats it exactly like ``no_quote``.
            return QuoteOutcome(
                key=contract_key, ok=False, reason=NO_QUOTE, source=source, latency_ms=latency_ms
            )
        reason = result.reason
        if reason != DEADLINE_EXCEEDED and reason not in (QUEUE_FULL, RUNTIME_CLOSED, CANCELLED):
            reason = WORKER_FAILED
        with self._lock:
            if result.reason == DEADLINE_EXCEEDED:
                self._counters.deadline_exceeded += 1
            elif reason == WORKER_FAILED:
                self._counters.errors += 1
        return QuoteOutcome(
            key=contract_key,
            ok=False,
            reason=reason,
            source=source,
            latency_ms=latency_ms,
            error=result.error,
        )


# ── Listener integration adapters ────────────────────────────────────────────
class PlaceableBroker(Protocol):
    """The one broker method the runtime needs; ``broker.py``'s brokers satisfy it."""

    def place(self, intent: OrderIntent) -> Ack: ...


@dataclass
class RuntimeQuoteProvider:
    """``QuoteProvider`` that puts a blocking feed call behind the bounded runtime.

    Wire it once - ``listener.quotes = RuntimeQuoteProvider(listener.quotes,
    runtime)`` - and every quote caller (the engine, ``_bids_for_book``,
    ``refresh_stops``) inherits the bounded, deadline-checked path. Disabled, it
    is the exact provider it wraps: no thread, no queue, no behaviour change.

    A deadline/queue refusal returns ``None``, which the engine already treats as
    ``no_quote``: the fail-closed direction. The named reason is on the runtime
    handle and the stage timer, so it is still visible after the fact.
    """

    provider: QuoteProvider
    runtime: BoundedRuntime
    deadline_ms: int = DEFAULT_QUOTE_DEADLINE_MS
    label: str = "quote"

    def quote(self, contract_key: str) -> Quote | None:
        if not self.runtime.enabled:
            return self.provider.quote(contract_key)
        outcome = self.runtime.call_quote(
            lambda: self.provider.quote(contract_key),
            deadline_ms=self.deadline_ms,
            label=f"{self.label}:{contract_key}",
        )
        if outcome.ok:
            return outcome.value
        return None


@dataclass
class RuntimeBroker:
    """``Broker`` adapter that bounds the blocking retry ladder.

    Wire it once at construction - ``listener.broker = RuntimeBroker(broker,
    runtime)`` - and the listener's existing taxonomy does the right thing with
    each refusal:

      * ``deadline_exceeded`` -> ``TransportError``: the venue may or may not have
        the order. The listener quarantines it as ``transport_unknown``, nothing
        retries, and ``recover`` settles it from a venue lookup. That is
        deliberately *not* a reject;
      * ``queue_full`` / ``runtime_closed`` -> ``BrokerError``: the order was
        never sent, so recording it as a business reject (never as a transport
        unknown) keeps the confirmed book honest;
      * the broker's own exception is re-raised unchanged.

    Disabled, ``place`` is exactly the wrapped broker's ``place``.
    """

    broker: PlaceableBroker
    runtime: BoundedRuntime
    deadline_ms: int = DEFAULT_PLACE_DEADLINE_MS

    def place(self, intent: OrderIntent) -> Ack:
        if not self.runtime.enabled:
            return self.broker.place(intent)
        handle = self.runtime.submit_place(
            lambda: self.broker.place(intent),
            deadline_ms=self.deadline_ms,
            label=f"place:{intent.contract_key}",
        )
        try:
            return handle.result()
        except StageDeadlineExceeded as exc:
            raise TransportError(
                f"deadline_exceeded: {intent.action} {intent.qty} "
                f"{intent.contract_key} may still be at the venue"
            ) from exc
        except (QueueFull, RuntimeClosed) as exc:
            raise BrokerError(
                f"runtime:{exc.reason}: {intent.action} {intent.qty} "
                f"{intent.contract_key}: order not sent"
            ) from exc


__all__ = [
    "CANCELLED",
    "DEADLINE_EXCEEDED",
    "DEFAULT_DEADLINE_MS",
    "DEFAULT_PLACE_DEADLINE_MS",
    "DEFAULT_QUEUE_CAPACITY",
    "DEFAULT_QUOTE_DEADLINE_MS",
    "DEFAULT_QUOTE_MAX_AGE_S",
    "DEFAULT_STALE_FLIGHT_S",
    "DEFAULT_WORKERS",
    "NO_CONTRACT",
    "NO_QUOTE",
    "QUEUE_FULL",
    "RUNTIME_CLOSED",
    "SOURCE_CACHE",
    "SOURCE_COALESCED",
    "SOURCE_FETCH",
    "SOURCE_REFUSED",
    "STAGES",
    "STAGE_DECISION",
    "STAGE_ORDER",
    "STAGE_QUOTE",
    "WORKER_FAILED",
    "BoundedRuntime",
    "PlaceableBroker",
    "QueueFull",
    "QuoteOutcome",
    "QuotePipeline",
    "QuotePipelineStats",
    "RuntimeBroker",
    "RuntimeClosed",
    "RuntimeFault",
    "RuntimeFuture",
    "RuntimeQuoteProvider",
    "RuntimeStats",
    "StageCancelled",
    "StageDeadlineExceeded",
    "StageResult",
    "StageState",
    "StageTimer",
    "run_in_worker",
]

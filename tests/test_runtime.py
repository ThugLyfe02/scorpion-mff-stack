"""Runtime tests: bounded workers, named refusals, single-flight quotes, timings.

The suite is offline and deterministic. Concurrency is driven by
``threading.Event`` handshakes rather than sleeps: the only real waits are the
deliberate deadlines (tens of milliseconds). No network, no broker, no tape.

Note: the shared pytest environment does not expose the ``pytest-asyncio``
plugin, so async cases use ``asyncio.run`` exactly like ``test_listener.py`` and
``test_gateway.py`` do. A fresh event loop per case also keeps the asyncio bridge
tests isolated.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from scorpion_mff.broker import Ack, BrokerError, PaperBroker, StaticQuotes, TransportError
from scorpion_mff.health import LatencyBudget
from scorpion_mff.runtime import (
    CANCELLED,
    DEADLINE_EXCEEDED,
    NO_QUOTE,
    QUEUE_FULL,
    RUNTIME_CLOSED,
    SOURCE_CACHE,
    SOURCE_FETCH,
    STAGE_DECISION,
    STAGE_ORDER,
    STAGE_QUOTE,
    WORKER_FAILED,
    BoundedRuntime,
    QuotePipeline,
    RuntimeBroker,
    RuntimeQuoteProvider,
    StageTimer,
    run_in_worker,
)
from scorpion_mff.types import OrderIntent, Quote

KEY = "QQQ|CALL|719|0DTE"
OTHER = "NVDA|PUT|227.5|2026-09-11"


def wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    """Poll a condition with a hard bound; deterministic except for the small sleeps."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


def ack(intent: OrderIntent) -> Ack:
    return Ack(
        ok=True,
        contract_key=intent.contract_key,
        qty=intent.qty,
        price=intent.limit,
        broker_order_id="test-1",
    )


def order(action: str = "buy", qty: int = 1, limit: float = 1.05) -> OrderIntent:
    return OrderIntent(
        action=action,
        qty=qty,
        limit=limit,
        contract_key=KEY,
        reason="test",
        idempotency_key="key-1",
    )


@dataclass
class SlowBroker:
    """A broker whose place() blocks until released; no transport, no network."""

    started: threading.Event
    release: threading.Event
    error: Exception | None = None

    def place(self, intent: OrderIntent) -> Ack:
        self.started.set()
        self.release.wait(2.0)
        if self.error is not None:
            raise self.error
        return ack(intent)


@dataclass
class SlowQuoteProvider:
    started: threading.Event
    release: threading.Event

    def quote(self, contract_key: str) -> Quote | None:
        self.started.set()
        self.release.wait(2.0)
        return Quote(bid=1.0, ask=1.1)


def blocked_runtime(
    *, workers: int = 1, queue_capacity: int = 4, deadline_ms: int = 2_000
) -> BoundedRuntime:
    return BoundedRuntime(
        enabled=True,
        workers=workers,
        queue_capacity=queue_capacity,
        default_deadline_ms=deadline_ms,
        name="mff-test",
    )


# ── default/off path ─────────────────────────────────────────────────────────
def test_disabled_runtime_is_inline_and_spawns_no_threads() -> None:
    runtime = BoundedRuntime()
    seen: list[str] = []
    assert runtime.enabled is False

    result = runtime.call(lambda: seen.append(threading.current_thread().name) or 42)

    assert result.ok and result.value == 42
    assert seen == [threading.current_thread().name]  # the caller's thread
    stats = runtime.stats()
    assert stats.threads == 0
    assert stats.completed == 1 and stats.submitted == 1


def test_disabled_runtime_still_records_the_stage_timer() -> None:
    runtime = BoundedRuntime()
    outcome = runtime.call_quote(lambda: 7)
    assert outcome.ok and outcome.value == 7
    assert runtime.timer.budget_for(STAGE_QUOTE).samples  # recorded
    assert runtime.stats().queue_depth == 0


def test_disabled_quote_provider_is_an_exact_passthrough() -> None:
    inner = StaticQuotes({KEY: (1.01, 1.05)})
    wrapped = RuntimeQuoteProvider(inner, BoundedRuntime())
    # `Quote.ts` is the read instant, so two reads legitimately differ by microseconds;
    # the passthrough contract is about the price the caller acts on, not the read time.
    direct, proxied = inner.quote(KEY), wrapped.quote(KEY)
    assert direct is not None and proxied is not None
    assert (proxied.bid, proxied.ask) == (direct.bid, direct.ask)
    assert wrapped.quote("MISSING") is None


def test_disabled_broker_is_an_exact_passthrough() -> None:
    paper = PaperBroker()
    wrapped = RuntimeBroker(paper, BoundedRuntime())
    placed = wrapped.place(order())
    assert paper.acks == [placed]

    class Exploding:
        def place(self, intent: OrderIntent) -> Ack:
            raise BrokerError("insufficient buying power")

    with pytest.raises(BrokerError, match="insufficient buying power"):
        RuntimeBroker(Exploding(), BoundedRuntime()).place(order())


def test_from_env_is_disabled_by_default_and_reads_explicit_config() -> None:
    assert BoundedRuntime.from_env({}).enabled is False

    configured = BoundedRuntime.from_env(
        {
            "MFF_RUNTIME": "1",
            "MFF_RUNTIME_WORKERS": "1",
            "MFF_RUNTIME_QUEUE": "2",
            "MFF_RUNTIME_DEADLINE_MS": "250",
        }
    )
    try:
        assert configured.enabled is True
        assert configured.workers == 1
        assert configured.queue_capacity == 2
        assert configured.default_deadline_ms == 250
    finally:
        configured.shutdown()


# ── bounded pool ─────────────────────────────────────────────────────────────
def test_enabled_runtime_runs_on_a_named_worker_thread() -> None:
    runtime = blocked_runtime(workers=1)
    try:
        result = runtime.call(lambda: threading.current_thread().name)
    finally:
        runtime.shutdown()
    assert result.ok
    assert str(result.value).startswith("mff-test-")


def test_queue_full_is_refused_with_its_named_reason_and_never_runs() -> None:
    runtime = blocked_runtime(workers=1, queue_capacity=1)
    started, release = threading.Event(), threading.Event()
    ran: list[str] = []
    try:
        busy = runtime.submit(lambda: (started.set(), release.wait(2.0), "busy")[2], 2_000, "busy")
        assert started.wait(2.0)
        queued = runtime.submit(lambda: "queued", 2_000, "queued")
        refused = runtime.submit(lambda: ran.append("ran") or "never", 2_000, "third")

        assert refused.refused is True
        outcome = refused.outcome()
        assert outcome.ok is False
        assert outcome.reason == QUEUE_FULL
        assert outcome.label == "third"
        assert ran == []  # the callable never ran
        assert runtime.stats().queue_full == 1

        release.set()
        assert busy.result(2_000) == "busy"
        assert queued.result(2_000) == "queued"
    finally:
        release.set()
        runtime.shutdown()


def test_a_full_queue_calls_the_refusal_hook_with_the_reason_and_label() -> None:
    refusals: list[tuple[str, str]] = []
    runtime = BoundedRuntime(
        enabled=True,
        workers=1,
        queue_capacity=1,
        on_refusal=lambda reason, label: refusals.append((reason, label)),
    )
    started, release = threading.Event(), threading.Event()
    try:
        runtime.submit(lambda: (started.set(), release.wait(2.0))[1], 2_000, "hold")
        assert started.wait(2.0)
        runtime.submit(lambda: None, 2_000, "queued")
        runtime.submit(lambda: None, 2_000, "refused")
        assert (QUEUE_FULL, "refused") in refusals
    finally:
        release.set()
        runtime.shutdown()


def test_deadline_exceeded_is_named_and_the_stage_keeps_running() -> None:
    runtime = blocked_runtime(workers=1, deadline_ms=80)
    started, release = threading.Event(), threading.Event()
    try:
        handle = runtime.submit(lambda: (started.set(), release.wait(2.0), "done")[2], 80, "slow")
        assert started.wait(2.0)

        first = handle.outcome()
        assert first.ok is False and first.reason == DEADLINE_EXCEEDED
        assert first.stage == STAGE_DECISION
        assert handle.done() is False  # non-terminal
        assert handle.deadline_exceeded is True
        assert runtime.stats().deadline_exceeded == 1

        release.set()
        second = handle.outcome(2_000)
        assert second.ok is True and second.value == "done"
        assert runtime.stats().completed == 1
    finally:
        release.set()
        runtime.shutdown()


def test_outcome_async_deadline_is_also_non_terminal() -> None:
    async def main() -> None:
        runtime = blocked_runtime(workers=1)
        started, release = threading.Event(), threading.Event()
        try:
            handle = runtime.submit(lambda: (started.set(), release.wait(2.0), 99)[2], 80, "slow")
            await asyncio.to_thread(started.wait, 2.0)

            first = await handle.outcome_async()
            assert first.reason == DEADLINE_EXCEEDED
            assert handle.done() is False

            release.set()
            second = await handle.outcome_async(2_000)
            assert second.ok and second.value == 99
        finally:
            release.set()
            runtime.shutdown()

    asyncio.run(main())


def test_a_worker_error_reports_the_original_exception() -> None:
    runtime = blocked_runtime(workers=1)

    def boom() -> None:
        raise BrokerError("rejected: not enough buying power")

    try:
        outcome = runtime.call(boom)
        assert outcome.ok is False
        assert outcome.reason == WORKER_FAILED
        assert isinstance(outcome.error, BrokerError)

        with pytest.raises(BrokerError, match="not enough buying power"):
            runtime.submit(boom).result()
    finally:
        runtime.shutdown()


def test_a_worker_error_does_not_kill_the_pool() -> None:
    runtime = blocked_runtime(workers=1)
    try:
        runtime.call(lambda: 1 // 0)
        after = runtime.call(lambda: "still alive")
    finally:
        runtime.shutdown()
    assert after.ok and after.value == "still alive"
    assert runtime.stats().failed == 1


def test_a_stage_cancelled_before_start_is_named() -> None:
    runtime = blocked_runtime(workers=1, queue_capacity=2)
    started, release = threading.Event(), threading.Event()
    try:
        busy = runtime.submit(lambda: (started.set(), release.wait(2.0))[1], 2_000, "busy")
        assert started.wait(2.0)
        queued = runtime.submit(lambda: "never", 2_000, "cancel-me")
        assert queued.cancel() is True
        assert queued.outcome().reason == CANCELLED
        release.set()
        busy.result(2_000)
        assert wait_until(lambda: runtime.stats().cancelled == 1)
    finally:
        release.set()
        runtime.shutdown()


def test_shutdown_drains_queued_jobs_and_refuses_new_work() -> None:
    runtime = blocked_runtime(workers=1, queue_capacity=4)
    started, release = threading.Event(), threading.Event()
    try:
        busy = runtime.submit(lambda: (started.set(), release.wait(2.0), "busy")[2], 2_000, "busy")
        assert started.wait(2.0)
        queued = runtime.submit(lambda: "queued", 2_000, "queued")

        runtime.shutdown(wait=False)
        drained = queued.outcome()
        assert drained.ok is False and drained.reason == RUNTIME_CLOSED
        assert runtime.closed is True

        refused = runtime.submit(lambda: "new", 2_000, "new")
        assert refused.refused is True
        assert refused.outcome().reason == RUNTIME_CLOSED

        release.set()
        assert busy.result(2_000) == "busy"
    finally:
        release.set()
        runtime.shutdown(wait=True, timeout_s=2.0)


def test_shutdown_on_a_disabled_runtime_refuses_new_work() -> None:
    runtime = BoundedRuntime()
    runtime.shutdown()
    outcome = runtime.submit(lambda: 1).outcome()
    assert outcome.reason == RUNTIME_CLOSED


def test_stats_track_submission_outcomes() -> None:
    runtime = blocked_runtime(workers=2)

    def boom() -> None:
        raise ValueError("nope")

    try:
        ok = runtime.submit(lambda: "ok", 2_000, "ok")
        bad = runtime.submit(boom, 2_000, "bad")
        ok.result(2_000)
        bad.outcome(2_000)
        stats = runtime.stats()
    finally:
        runtime.shutdown()
    assert stats.submitted == 2
    assert stats.completed == 1
    assert stats.failed == 1
    assert stats.queue_full == 0
    assert stats.threads == 2
    assert stats.peak_in_flight >= 1


def test_validates_its_bounds_instead_of_silently_unbounding() -> None:
    with pytest.raises(ValueError):
        BoundedRuntime(enabled=True, workers=0)
    with pytest.raises(ValueError):
        BoundedRuntime(enabled=True, queue_capacity=0)
    with pytest.raises(ValueError):
        BoundedRuntime(enabled=True, default_deadline_ms=0)
    with pytest.raises(ValueError):
        BoundedRuntime().submit(lambda: None, deadline_ms=0)


def test_unrelated_work_proceeds_while_one_stage_is_slow() -> None:
    runtime = blocked_runtime(workers=2)
    started, release = threading.Event(), threading.Event()
    try:
        slow = runtime.submit(lambda: (started.set(), release.wait(2.0), "slow")[2], 2_000, "slow")
        assert started.wait(2.0)
        fast = runtime.call(lambda: "fast", stage=STAGE_QUOTE, deadline_ms=2_000)
        assert fast.ok and fast.value == "fast"
        assert slow.done() is False
        release.set()
        assert slow.result(2_000) == "slow"
    finally:
        release.set()
        runtime.shutdown()


# ── stage timer ──────────────────────────────────────────────────────────────
def test_stage_timer_records_per_stage_and_total_latency() -> None:
    timer = StageTimer(budget=LatencyBudget(budget_ms=1_000))
    assert timer.record(STAGE_QUOTE, 10) is True
    assert timer.record(STAGE_QUOTE, 20) is True
    assert timer.record(STAGE_ORDER, 5) is True

    snapshot = timer.snapshot()
    stages = snapshot["stages"]
    total = snapshot["total"]
    assert stages[STAGE_QUOTE]["n"] == 2  # type: ignore[index]
    assert stages[STAGE_QUOTE]["worst_ms"] == 20  # type: ignore[index]
    assert stages[STAGE_ORDER]["n"] == 1  # type: ignore[index]
    assert total["n"] == 3  # type: ignore[index]
    assert timer.worst_stage() == STAGE_QUOTE
    assert timer.breaches == 0


def test_stage_timer_breach_is_named_through_the_hook() -> None:
    breaches: list[tuple[str, int, int]] = []
    timer = StageTimer(
        budget=LatencyBudget(budget_ms=50),
        on_breach=lambda stage, latency, budget_ms: breaches.append((stage, latency, budget_ms)),
    )
    assert timer.record(STAGE_QUOTE, 10) is True
    assert timer.record(STAGE_QUOTE, 60) is False
    assert breaches == [(STAGE_QUOTE, 60, 50)]
    assert timer.breaches == 1


def test_stage_timer_span_uses_the_injected_clock() -> None:
    now = [0.0]
    timer = StageTimer(clock=lambda: now[0])
    with timer.span(STAGE_ORDER):
        now[0] += 0.25
    assert timer.budget_for(STAGE_ORDER).samples == [250]
    assert timer.budget.samples == [250]


def test_stage_timer_snapshot_is_json_ready() -> None:
    timer = StageTimer(budget=LatencyBudget(budget_ms=1_000))
    timer.record(STAGE_DECISION, 4)
    encoded = json.dumps(timer.snapshot())
    assert '"decision"' in encoded and '"worst_stage"' in encoded


# ── sync -> async bridge ─────────────────────────────────────────────────────
def test_run_in_worker_keeps_the_event_loop_free() -> None:
    async def main() -> None:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.002)
                ticks += 1

        task = asyncio.create_task(ticker())
        result = await run_in_worker(lambda: (time.sleep(0.04), 7)[1], timeout_ms=1_000)
        await task
        assert result.ok and result.value == 7
        assert ticks == 20

    asyncio.run(main())


def test_run_in_worker_names_a_deadline_without_killing_the_thread() -> None:
    async def main() -> None:
        runtime = blocked_runtime(workers=1)
        started, release = threading.Event(), threading.Event()
        try:
            task = asyncio.create_task(
                run_in_worker(
                    lambda: (started.set(), release.wait(2.0), "done")[2],
                    timeout_ms=80,
                    runtime=runtime,
                    label="order",
                )
            )
            await asyncio.to_thread(started.wait, 2.0)
            result = await task
            assert result.ok is False and result.reason == DEADLINE_EXCEEDED

            release.set()
            assert await asyncio.to_thread(wait_until, lambda: runtime.stats().completed == 1)
        finally:
            release.set()
            runtime.shutdown()

    asyncio.run(main())


# ── single-flight quote pipeline ─────────────────────────────────────────────
def test_quote_pipeline_collapses_concurrent_requests_for_one_contract() -> None:
    async def main() -> None:
        calls = 0
        release = threading.Event()

        def fetch(contract_key: str) -> Quote:
            nonlocal calls
            calls += 1
            release.wait(2.0)
            return Quote(bid=1.0, ask=1.1)

        pipeline: QuotePipeline[Quote] = QuotePipeline(fetch, max_age_s=60.0)
        try:
            tasks = [asyncio.create_task(pipeline.aquote(KEY)) for _ in range(6)]
            assert await asyncio.to_thread(
                wait_until, lambda: pipeline.stats().fetches + pipeline.stats().coalesced >= 6
            )
            release.set()
            outcomes = await asyncio.gather(*tasks)
        finally:
            release.set()
            pipeline.close()
        assert calls == 1
        assert all(outcome.ok for outcome in outcomes)
        assert pipeline.stats().coalesced == 5
        assert pipeline.stats().fetches == 1

    asyncio.run(main())


def test_a_slow_quote_for_one_contract_does_not_block_another() -> None:
    async def main() -> None:
        started, release = threading.Event(), threading.Event()

        def fetch(contract_key: str) -> Quote:
            if contract_key == KEY:
                started.set()
                release.wait(2.0)
            return Quote(bid=1.0, ask=1.1)

        pipeline: QuotePipeline[Quote] = QuotePipeline(fetch, max_age_s=0.0)
        try:
            slow = asyncio.create_task(pipeline.aquote(KEY))
            await asyncio.to_thread(started.wait, 2.0)

            fast = await asyncio.wait_for(pipeline.aquote(OTHER), 2.0)
            assert fast.ok is True
            assert slow.done() is False

            release.set()
            held = await asyncio.wait_for(slow, 2.0)
            assert held.ok is True
        finally:
            release.set()
            pipeline.close(wait=True, timeout_s=2.0)

    asyncio.run(main())


def test_quote_pipeline_cache_serves_within_max_age() -> None:
    now = [1_000.0]
    calls = 0

    def fetch(contract_key: str) -> Quote:
        nonlocal calls
        calls += 1
        return Quote(bid=1.0, ask=1.1)

    pipeline: QuotePipeline[Quote] = QuotePipeline(
        fetch, runtime=BoundedRuntime(), clock=lambda: now[0], max_age_s=5.0
    )
    first = pipeline.quote(KEY)
    second = pipeline.quote(KEY)
    assert first.ok and second.ok
    assert second.source == SOURCE_CACHE
    assert calls == 1
    assert pipeline.stats().cache_hits == 1


def test_quote_pipeline_cache_expires() -> None:
    now = [1_000.0]
    calls = 0

    def fetch(contract_key: str) -> Quote:
        nonlocal calls
        calls += 1
        return Quote(bid=1.0, ask=1.1)

    pipeline: QuotePipeline[Quote] = QuotePipeline(
        fetch, runtime=BoundedRuntime(), clock=lambda: now[0], max_age_s=5.0
    )
    pipeline.quote(KEY)
    now[0] += 6.0
    refreshed = pipeline.quote(KEY)
    assert refreshed.ok and refreshed.source == SOURCE_FETCH
    assert calls == 2


def test_quote_pipeline_names_an_unpriceable_contract() -> None:
    pipeline: QuotePipeline[Quote] = QuotePipeline(
        lambda contract_key: None, runtime=BoundedRuntime(), max_age_s=5.0
    )
    outcome = pipeline.quote(KEY)
    assert outcome.ok is False and outcome.reason == NO_QUOTE
    assert pipeline.stats().no_quote == 1


def test_quote_pipeline_names_a_deadline() -> None:
    started, release = threading.Event(), threading.Event()

    def fetch(contract_key: str) -> Quote:
        started.set()
        release.wait(2.0)
        return Quote(bid=1.0, ask=1.1)

    pipeline: QuotePipeline[Quote] = QuotePipeline(fetch, deadline_ms=80)
    runtime = pipeline.runtime
    try:
        outcome = pipeline.quote(KEY)
        assert outcome.ok is False and outcome.reason == DEADLINE_EXCEEDED
        assert pipeline.stats().deadline_exceeded == 1
    finally:
        release.set()
        assert wait_until(lambda: runtime.stats().completed == 1)
        pipeline.close(wait=True, timeout_s=2.0)


def test_quote_pipeline_abandons_a_stale_flight() -> None:
    now = [1_000.0]
    calls = 0
    release = threading.Event()

    def fetch(contract_key: str) -> Quote:
        nonlocal calls
        calls += 1
        if calls == 1:
            release.wait(2.0)
        return Quote(bid=1.0, ask=1.1)

    runtime = BoundedRuntime(enabled=True, workers=2, clock=lambda: now[0])
    pipeline: QuotePipeline[Quote] = QuotePipeline(
        fetch,
        runtime=runtime,
        clock=lambda: now[0],
        max_age_s=0.0,
        stale_flight_s=10.0,
        deadline_ms=2_000,
    )
    try:
        first = pipeline.quote(KEY, deadline_ms=80)
        assert first.ok is False and first.reason == DEADLINE_EXCEEDED

        now[0] += 20.0  # old flight is now stale
        second = pipeline.quote(KEY, deadline_ms=2_000)
        assert second.ok is True
        assert pipeline.stats().stale_flights == 1
        assert calls == 2
    finally:
        release.set()
        assert wait_until(lambda: runtime.stats().completed >= 2)
        pipeline.close(wait=True, timeout_s=2.0)


def test_quote_pipeline_names_a_queue_full_refusal() -> None:
    runtime = blocked_runtime(workers=1, queue_capacity=1)
    started, release = threading.Event(), threading.Event()
    try:
        runtime.submit(lambda: (started.set(), release.wait(2.0))[1], 2_000, "hold")
        assert started.wait(2.0)
        runtime.submit(lambda: None, 2_000, "queued")

        pipeline: QuotePipeline[Quote] = QuotePipeline(
            lambda contract_key: Quote(bid=1.0, ask=1.1), runtime=runtime
        )
        outcome = pipeline.quote(KEY)
        assert outcome.ok is False and outcome.reason == QUEUE_FULL
    finally:
        release.set()
        runtime.shutdown()


# ── Listener adapters ────────────────────────────────────────────────────────
def test_runtime_quote_provider_bounds_a_slow_feed_and_fails_closed() -> None:
    runtime = blocked_runtime(workers=1)
    provider = SlowQuoteProvider(threading.Event(), threading.Event())
    wrapped = RuntimeQuoteProvider(provider, runtime, deadline_ms=80)
    try:
        assert wrapped.quote(KEY) is None  # deadline -> no_quote -> None
        provider.release.set()
        assert wait_until(lambda: runtime.stats().completed == 1)
    finally:
        provider.release.set()
        runtime.shutdown()


def test_runtime_broker_maps_a_deadline_to_transport_unknown() -> None:
    runtime = blocked_runtime(workers=1)
    broker = SlowBroker(threading.Event(), threading.Event())
    wrapped = RuntimeBroker(broker, runtime, deadline_ms=80)
    try:
        with pytest.raises(TransportError, match="deadline_exceeded") as info:
            wrapped.place(order())
        assert type(info.value) is TransportError  # non-terminal ambiguity
        broker.release.set()
        assert wait_until(lambda: runtime.stats().completed == 1)
    finally:
        broker.release.set()
        runtime.shutdown()


def test_runtime_broker_maps_a_full_queue_to_a_business_refusal() -> None:
    runtime = blocked_runtime(workers=1, queue_capacity=1)
    started, release = threading.Event(), threading.Event()
    try:
        runtime.submit(lambda: (started.set(), release.wait(2.0))[1], 2_000, "hold")
        assert started.wait(2.0)
        runtime.submit(lambda: None, 2_000, "queued")

        wrapped = RuntimeBroker(PaperBroker(), runtime)
        with pytest.raises(BrokerError, match="queue_full") as info:
            wrapped.place(order())
        assert type(info.value) is BrokerError  # never sent -> not transport
    finally:
        release.set()
        runtime.shutdown()

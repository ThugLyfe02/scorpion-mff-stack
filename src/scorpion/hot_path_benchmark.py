from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .accuracy import percentile
from .config import GUILD_ID
from .domain import RawDiscordMessage
from .durable_ingress import append_raw_with_receipt
from .pipeline import Pipeline
from .processing_order import inspect_processing_order
from .profiling import profile_messages
from .provenance import stable_hash
from .session_service import Journal, JournalError, verify_paper_journal
from .stage_trace import load_stage_latency_report
from .store import Store


@dataclass(frozen=True, slots=True)
class BenchmarkPercentiles:
    p50_us: float
    p95_us: float
    p99_us: float
    maximum_us: int


@dataclass(frozen=True, slots=True)
class HotPathBenchmarkPolicy:
    minimum_samples: int = 30
    maximum_core_p95_us: float = 5_000.0
    maximum_core_p99_us: float = 15_000.0
    maximum_receipt_p95_us: float = 15_000.0
    maximum_full_path_p95_us: float = 75_000.0
    maximum_full_path_p99_us: float = 150_000.0
    maximum_db_precommit_p95_us: float = 25_000.0

    def __post_init__(self) -> None:
        if self.minimum_samples <= 0:
            raise ValueError("minimum_samples must be positive")
        for name in (
            "maximum_core_p95_us",
            "maximum_core_p99_us",
            "maximum_receipt_p95_us",
            "maximum_full_path_p95_us",
            "maximum_full_path_p99_us",
            "maximum_db_precommit_p95_us",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class HotPathBenchmarkReport:
    benchmark_id: str
    generated_ts_utc: datetime
    samples: int
    core: BenchmarkPercentiles
    durable_receipt: BenchmarkPercentiles
    full_path: BenchmarkPercentiles
    db_precommit_p95_us: float
    atomic_receipt_order_verified: bool
    final_state_fingerprint: str
    python_version: str
    platform: str
    sqlite_version: str
    journal_mode: str
    synchronous_mode: int
    passed: bool
    failures: tuple[str, ...]
    core_event_kind_counts: tuple[tuple[str, int], ...]


def _summary(values: list[int]) -> BenchmarkPercentiles:
    if not values:
        return BenchmarkPercentiles(0.0, 0.0, 0.0, 0)
    return BenchmarkPercentiles(
        p50_us=percentile(values, 0.50),
        p95_us=percentile(values, 0.95),
        p99_us=percentile(values, 0.99),
        maximum_us=max(values),
    )


def _message(
    *,
    index: int,
    when: datetime,
    content: str,
    channel_id: str = "968352649437126676",
) -> RawDiscordMessage:
    timestamp = when + timedelta(milliseconds=index)
    return RawDiscordMessage(
        message_id=f"benchmark-{index}",
        guild_id=GUILD_ID,
        channel_id=channel_id,
        author_id="benchmark-author",
        content=content,
        source_ts_utc=timestamp,
        received_ts_utc=timestamp,
    )


def _core_corpus(samples: int, when: datetime) -> tuple[RawDiscordMessage, ...]:
    templates = (
        "QQQ 719C TODAY @ 1.01",
        "Printing +19%",
        "NKE 42.5C Sep 11 @ 1.40",
    )
    return tuple(
        _message(index=index, when=when, content=templates[index % len(templates)])
        for index in range(samples)
    )


def _full_path_corpus(samples: int, when: datetime) -> tuple[RawDiscordMessage, ...]:
    # Wrong-channel events still traverse durable receipt, parser, decision packet, reducer
    # validation, integrity evidence and normalized SQLite commit without accumulating positions.
    return tuple(
        _message(
            index=100_000 + index,
            when=when,
            content="QQQ 719C TODAY @ 1.01",
            channel_id="benchmark-non-execution-channel",
        )
        for index in range(samples)
    )


def _sqlite_modes(path: Path) -> tuple[str, int]:
    with sqlite3.connect(str(path)) as db:
        journal_mode = str(db.execute("PRAGMA journal_mode").fetchone()[0])
        synchronous_mode = int(db.execute("PRAGMA synchronous").fetchone()[0])
    return journal_mode, synchronous_mode


async def _run_full_path(
    store: Store,
    pipeline: Pipeline,
    messages: tuple[RawDiscordMessage, ...],
) -> tuple[list[int], list[int]]:
    receipt_latencies: list[int] = []
    full_latencies: list[int] = []
    for raw in messages:
        started = time.perf_counter_ns()
        receipt_started = time.perf_counter_ns()
        inserted = await asyncio.to_thread(append_raw_with_receipt, store.path, raw)
        receipt_latencies.append(max(0, (time.perf_counter_ns() - receipt_started) // 1_000))
        if not inserted:
            raise RuntimeError("benchmark generated a duplicate raw revision")
        await asyncio.to_thread(
            pipeline._process_serialized,
            raw,
            persist_raw=False,
            register_receipt=False,
        )
        full_latencies.append(max(0, (time.perf_counter_ns() - started) // 1_000))
    return receipt_latencies, full_latencies


def benchmark_hot_path(
    *,
    samples: int = 80,
    workspace: str | Path | None = None,
    now: datetime | None = None,
    policy: HotPathBenchmarkPolicy | None = None,
) -> HotPathBenchmarkReport:
    """Benchmark pure decision compute and the durable normalized path on an isolated store."""
    policy = policy or HotPathBenchmarkPolicy()
    if samples < policy.minimum_samples:
        raise ValueError(f"samples must be >= {policy.minimum_samples}")
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    failures: list[str] = []

    core_profile = profile_messages(
        _core_corpus(samples, timestamp),
        allowed_author_ids=frozenset({"benchmark-author"}),
    )
    core_counts = Counter(sample.kind.value for sample in core_profile.samples)
    if core_counts.get("ENTRY", 0) == 0:
        failures.append("core_fixture_did_not_exercise_entry")
    core = BenchmarkPercentiles(
        p50_us=core_profile.total.p50_us,
        p95_us=core_profile.total.p95_us,
        p99_us=core_profile.total.p99_us,
        maximum_us=max((sample.total_us for sample in core_profile.samples), default=0),
    )

    suffix = hashlib.sha256(timestamp.isoformat().encode()).hexdigest()[:16]
    if workspace is None:
        context = tempfile.TemporaryDirectory(prefix=f"scorpion-hot-path-{suffix}-")
    else:
        root = Path(workspace)
        root.mkdir(parents=True, exist_ok=True)
        context = tempfile.TemporaryDirectory(prefix=f"hot-path-{suffix}-", dir=root)
    db_path = Path(context.name) / "benchmark.db"

    try:
        store = Store(db_path)
        pipeline = Pipeline(
            store,
            allowed_author_ids=frozenset({"benchmark-author"}),
        )
        receipt_values, full_values = asyncio.run(
            _run_full_path(store, pipeline, _full_path_corpus(samples, timestamp))
        )
        receipt = _summary(receipt_values)
        full_path = _summary(full_values)
        order = inspect_processing_order(db_path)
        atomic_receipt_verified = (
            order.complete
            and order.raw_count == samples
            and order.receipt_ordered_count == samples
            and order.signal_count == samples
            and order.process_ordered_count == samples
        )
        stage = load_stage_latency_report(db_path, limit=samples)
        db_precommit = stage.stages.get("db_precommit_us")
        db_precommit_p95_us = db_precommit.p95_us if db_precommit is not None else 0.0
        journal_mode, synchronous_mode = _sqlite_modes(db_path)
    finally:
        context.cleanup()

    if core.p95_us > policy.maximum_core_p95_us:
        failures.append("core_p95_budget_exceeded")
    if core.p99_us > policy.maximum_core_p99_us:
        failures.append("core_p99_budget_exceeded")
    if receipt.p95_us > policy.maximum_receipt_p95_us:
        failures.append("durable_receipt_p95_budget_exceeded")
    if full_path.p95_us > policy.maximum_full_path_p95_us:
        failures.append("full_path_p95_budget_exceeded")
    if full_path.p99_us > policy.maximum_full_path_p99_us:
        failures.append("full_path_p99_budget_exceeded")
    if db_precommit_p95_us > policy.maximum_db_precommit_p95_us:
        failures.append("db_precommit_p95_budget_exceeded")
    if not atomic_receipt_verified:
        failures.append("atomic_receipt_processing_order_not_verified")
    if journal_mode.lower() != "wal":
        failures.append("benchmark_store_not_wal")
    if synchronous_mode < 2:
        failures.append("benchmark_store_not_full_sync")

    material = {
        "version": "hot-path-benchmark-v2",
        "generated_ts_utc": timestamp.isoformat(),
        "samples": samples,
        "core": asdict(core),
        "core_event_kind_counts": sorted(core_counts.items()),
        "durable_receipt": asdict(receipt),
        "full_path": asdict(full_path),
        "db_precommit_p95_us": db_precommit_p95_us,
        "atomic_receipt_order_verified": atomic_receipt_verified,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "sqlite_version": sqlite3.sqlite_version,
        "journal_mode": journal_mode,
        "synchronous_mode": synchronous_mode,
        "failures": failures,
    }
    benchmark_id = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return HotPathBenchmarkReport(
        benchmark_id=benchmark_id,
        generated_ts_utc=timestamp,
        samples=samples,
        core=core,
        durable_receipt=receipt,
        full_path=full_path,
        db_precommit_p95_us=db_precommit_p95_us,
        atomic_receipt_order_verified=atomic_receipt_verified,
        final_state_fingerprint=core_profile.final_state_fingerprint,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        sqlite_version=sqlite3.sqlite_version,
        journal_mode=journal_mode,
        synchronous_mode=synchronous_mode,
        passed=not failures,
        failures=tuple(failures),
        core_event_kind_counts=tuple(sorted(core_counts.items())),
    )


@dataclass(frozen=True, slots=True)
class CaptureBenchmarkReport:
    requested_records: int
    admitted_records: int
    durable_records: int
    records_per_second: float
    bytes_per_second: float
    producer_admission: BenchmarkPercentiles
    admission_to_checkpoint: BenchmarkPercentiles
    end_to_end_seconds: float
    byte_count: int
    exact_receipt_verified: bool
    workload_sha256: str
    batch_size: int
    producer_rate: int
    python_version: str
    platform: str
    failures: tuple[str, ...]
    authority: str = "RESEARCH_ONLY"
    production_capacity_certified: bool = False


def benchmark_paper_capture(
    *, samples: int = 2048, batch: int = 64, producer_rate: int = 0,
    workspace: Path | None = None,
) -> CaptureBenchmarkReport:
    """Real strict journal fsync + checkpoint path; synthetic fixed-size market records.

    producer_rate=0 is an unpaced burst. The exact admitted/acknowledged sequence
    and receipt are verified; no loss is hidden in the throughput denominator.
    """
    if (
        not 32 <= samples <= 80_000 or not 1 <= batch <= 256
        or not 0 <= producer_rate <= 100_000
        or (producer_rate and samples / producer_rate > 60)
    ):
        raise ValueError("capture benchmark outside bounded workload")
    records = tuple({
        "kind": "market_quote", "symbol": f"BENCH{i % 32:02d}",
        "ts_event_ns": 1_800_000_000_000_000_000 + i,
        "recv_wall_ns": 1_800_000_000_001_000_000 + i,
        "recv_mono_ns": 1_000_000 + i, "bid": "9.99", "ask": "10.01",
        "bid_size": 100 + i % 20, "ask_size": 100 + i % 50,
        "publisher_id": 1, "instrument_id": i % 32 + 1, "provider_sequence": i + 1,
    } for i in range(samples))
    workload = stable_hash(records)
    submit_ns: dict[int, int] = {}
    durable_ns: dict[int, int] = {}
    acknowledgements: list[int] = []
    durations: list[int] = []
    lock = threading.Lock()
    failures: list[str] = []

    def acknowledged(seq: int, stamp: int) -> None:
        with lock:
            acknowledgements.append(seq)
            durable_ns[seq] = stamp

    with tempfile.TemporaryDirectory(prefix="paper-capture-bench-", dir=workspace) as folder:
        path = Path(folder) / "capture.jsonl"
        journal = Journal(path, batch=batch, require_lossless=True, on_durable=acknowledged)
        started = time.perf_counter_ns()
        admitted = 0
        for record in records:
            if producer_rate:
                target = started + admitted * 1_000_000_000 // producer_rate
                remaining = (target - time.perf_counter_ns()) / 1e9
                if remaining > 0:
                    time.sleep(remaining)
            tick = time.perf_counter_ns()
            try:
                seq = journal.submit(record)
            except JournalError as exc:
                failures.append(str(exc))
                break
            with lock:
                submit_ns[seq] = tick
            durations.append((time.perf_counter_ns() - tick) // 1000)
            admitted += 1
        try:
            journal.close(timeout_s=30)
        except JournalError as exc:
            failures.append(str(exc))
        elapsed = max((time.perf_counter_ns() - started) / 1e9, 1e-9)
        verified = False
        size = path.stat().st_size if path.exists() else 0
        try:
            receipt = verify_paper_journal(path)
            verified = (
                receipt["records"] == samples == admitted == len(acknowledgements)
                and acknowledgements == list(range(1, samples + 1))
            )
        except JournalError:
            failures.append("capture_receipt_unverified")
        if not verified:
            failures.append("capture_loss_or_incomplete_workload")
        with lock:
            latencies = [(stamp - submit_ns[seq]) // 1000 for seq, stamp in durable_ns.items()
                         if seq in submit_ns]
        return CaptureBenchmarkReport(
            samples, admitted, len(acknowledgements), len(acknowledgements) / elapsed,
            size / elapsed, _summary(durations), _summary(latencies), elapsed, size,
            verified, workload, batch, producer_rate, sys.version.split()[0],
            platform.platform(), tuple(dict.fromkeys(failures)),
        )

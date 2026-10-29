"""The long-running dispatcher: the operator surface the CLI does not have.

A control plane that must act inside a latency budget cannot depend on a human re-running
`scorpion dispatch` every second, and minute-granularity cron is the difference between the
pipe test and a missed exit. This module is the service form of the same pass `cmd_dispatch`
runs, plus the three properties a loop needs and a one-shot does not:

1. **Jittered cadence.** N workers waking on the same second contend for one SQLite write;
   jitter spreads them. The sleep after an idle or blocked pass grows exponentially to a
   cap, so a quiet market does not become a hot loop.
2. **Automatic reconciliation.** UNKNOWN sends were reported and then waited for a human.
   The loop resolves them against the venue lookup on every tick they exist, and it never
   blind-retries - the tri-state lookup is the only retry authority.
3. **A metrics series.** `ops.collect` is one point in time. A series is the history of
   those points, one bounded row per tick, so "did the dispatcher stop dispatching" is a
   question a file answers rather than a guess.

The pass itself is identical to `cmd_dispatch`: rebuild the book from its applied log,
recover any send a crash left in flight, run one outbox pass, settle any fill, and record
the outcome. The difference is that it happens again, on its own, until told to stop - and
it refuses to stop quietly while the desk is halted or a send's fate is unknown.

Exit codes (for cron, systemd, or Task Scheduler):

    0  stopped clean, nothing outstanding
    1  stopped with a problem an operator must look at (halt, unknown send, stale log)
    2  the service itself could not run (snapshot corrupt, store unreadable)
"""

from __future__ import annotations

import contextlib
import json
import random
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .broker import Quote
from .config import MARKET_TZ, Policy
from .ops import Liveness, liveness
from .outbox import Dispatched, Dispatcher, DispatchReport, Sender
from .pipeline import Pipeline
from .reconciliation import (
    NoLookup,
    OrderLookup,
    UnknownReport,
    reconcile_unknown,
)
from .snapshot import SnapshotError, state_from_snapshot
from .store import Store


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Every knob the loop needs, none of it credential-gated."""

    cadence_seconds: float = 1.0
    jitter_seconds: float = 0.25
    idle_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 60.0
    max_ticks: int | None = None
    owner: str = "service"
    live: bool = False
    series_path: Path | None = None
    series_cap: int = 10_000
    reconcile_on_unknown: bool = True
    recover_after_seconds: int = 0
    stale_after_seconds: int = 1800

    def __post_init__(self) -> None:
        if self.cadence_seconds < 0 or self.jitter_seconds < 0:
            raise ValueError("cadence and jitter must not be negative")
        if self.idle_backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("backoff must not be negative")
        if self.series_cap < 1:
            raise ValueError("series_cap must be positive")


# ── the metrics series ──────────────────────────────────────────────────────
class SeriesLog:
    """A bounded JSONL history of tick reports.

    Appends are single-line writes, so a crash between ticks loses at most the record being
    written, never the file. Compaction runs only when the file is double the cap, rewrites
    to the last `cap` records through a temp file, and replaces atomically - a reader never
    sees a half-compacted series.
    """

    def __init__(self, path: Path | None, *, cap: int = 10_000) -> None:
        self.path = path
        self.cap = cap
        self.written = 0

    def append(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        self.written += 1
        if self.written > self.cap * 2:
            self._compact()

    def _compact(self) -> None:
        if self.path is None:
            return
        tail = read_series(self.path)[-self.cap :]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in tail),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self.written = len(tail)


def read_series(path: Path, *, tail: int | None = None) -> list[dict[str, Any]]:
    """Read the series back. `tail` returns only the last N records, newest last."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records[-tail:] if tail else records


def summarize_series(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Cumulative totals and rates, plus the last record so the tail is one read away."""
    if not records:
        return {"ticks": 0, "halt_observed": False}
    dispatched = {"attempted": 0, "sent": 0, "refused": 0, "retried": 0, "dead": 0}
    reconciled_sent = 0
    reconciled_retried = 0
    unknown_max = 0
    for record in records:
        d = record.get("dispatched") or {}
        for key in dispatched:
            dispatched[key] += int(d.get(key) or 0)
        reconciliation = record.get("reconciliation") or {}
        reconciled_sent += int(reconciliation.get("sent") or 0)
        reconciled_retried += int(reconciliation.get("retried") or 0)
        unknown_max = max(unknown_max, int(record.get("unknown_after") or 0))
    first_ts = str(records[0].get("ts_utc") or "")
    last_ts = str(records[-1].get("ts_utc") or "")
    span_seconds: float | None = None
    try:
        span_seconds = (
            datetime.fromisoformat(last_ts) - datetime.fromisoformat(first_ts)
        ).total_seconds()
    except ValueError:
        span_seconds = None
    minutes = (span_seconds or 0) / 60.0 if span_seconds else None
    return {
        "ticks": len(records),
        "first_ts_utc": first_ts or None,
        "last_ts_utc": last_ts or None,
        "span_seconds": span_seconds,
        "dispatched": dispatched,
        "reconciled": {"sent": reconciled_sent, "retried": reconciled_retried},
        "unknown_max": unknown_max,
        "pending_unknown_now": int(records[-1].get("unknown_after") or 0),
        "halt_observed": any(bool(record.get("halted")) for record in records),
        "sent_per_minute": round(dispatched["sent"] / minutes, 3) if minutes else None,
        "last": records[-1],
    }


def render_series(summary: dict[str, Any]) -> str:
    if summary.get("ticks") == 0:
        return "no series records"
    dispatched = summary.get("dispatched") or {}
    reconciled = summary.get("reconciled") or {}
    lines = [
        f"ticks       {summary['ticks']}  span {summary.get('span_seconds')}s",
        f"dispatched  sent {dispatched.get('sent')} refused {dispatched.get('refused')}"
        f" dead {dispatched.get('dead')}",
        f"reconciled  sent {reconciled.get('sent')} retried {reconciled.get('retried')}",
        f"unknown     max {summary.get('unknown_max')} now {summary.get('pending_unknown_now')}",
        f"halt seen   {summary.get('halt_observed')}",
    ]
    if summary.get("sent_per_minute") is not None:
        lines.append(f"rate        {summary['sent_per_minute']} sends/min")
    return "\n".join(lines)


# ── the tick ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class TickReport:
    """One bounded pass, everything it saw, and the exit code an operator should read."""

    now: datetime
    dispatched: DispatchReport
    reconciliation: UnknownReport | None
    halted: bool
    halt_reason: str
    unknown_before: int
    unknown_after: int
    liveness: Liveness
    circuit_open: bool
    problems: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return 1 if self.problems else 0

    def to_series(self) -> dict[str, object]:
        d = self.dispatched
        return {
            "ts_utc": self.now.isoformat(),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "dispatched": {
                "attempted": d.attempted, "sent": d.sent, "refused": d.refused,
                "retried": d.retried, "dead": d.dead,
            },
            "unknown_before": self.unknown_before,
            "unknown_after": self.unknown_after,
            "reconciliation": self.reconciliation.to_dict() if self.reconciliation else None,
            "liveness": self.liveness.status,
            "last_applied_age_s": (
                int(self.liveness.age.total_seconds()) if self.liveness.age else None
            ),
            "circuit_open": self.circuit_open,
            "exit_code": self.exit_code,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.to_series(),
            "problems": list(self.problems),
        }


# ── the service ─────────────────────────────────────────────────────────────
class Service:
    """The loop. `tick` is pure; `step` adds the record; `run` loops until stopped."""

    def __init__(
        self,
        store: Store,
        *,
        sender: Sender,
        lookup: OrderLookup | None = None,
        policy: Policy | None = None,
        quotes: Mapping[str, Quote] | None = None,
        references: Mapping[str, Decimal] | None = None,
        config: ServiceConfig | None = None,
        seed: int | None = None,
    ) -> None:
        self.store = store
        self.config = config or ServiceConfig()
        self.sender = sender
        self.lookup = lookup or NoLookup()
        self.policy = policy or Policy(live_submission_enabled=self.config.live)
        self.quotes = quotes or {}
        self.references = references or {}
        self.rng = random.Random(seed)
        # The pipeline is the home of `settle`, which moves the book and the risk ledger
        # together. Its state is overridden from the applied log every tick, so the loop
        # authorizes against what the venue actually holds rather than a re-derived intent.
        self.pipeline = Pipeline(store=store)
        self.dispatcher = Dispatcher(
            store=store, sender=sender, policy=self.policy, owner=self.config.owner,
            on_filled=self._settle,
            recover_after_seconds=self.config.recover_after_seconds,
        )
        self.series = SeriesLog(self.config.series_path, cap=self.config.series_cap)
        self._stop = False
        self._idle_streak = 0
        self._tick_index = 0
        self._market_day: date | None = None

    def request_stop(self) -> None:
        self._stop = True

    # ── one pass ────────────────────────────────────────────────────────────
    def tick(self, *, now: datetime | None = None) -> TickReport:
        moment = now or datetime.now(UTC)
        state, _ = state_from_snapshot(self.store)
        self.pipeline.state = state
        self.pipeline.last_market_day = state.risk.market_day
        self._market_day = state.risk.market_day or moment.astimezone(MARKET_TZ).date()

        halted, halt_reason = self.store.halt_state()
        halted = halted or state.halted
        if not halt_reason:
            halt_reason = state.risk.halted_reason or ""

        # A SENDING row at tick start is an in-flight send from a process that died; it is
        # quarantined before anything else so it can never be blind-retried.
        self.store.recover_sending(
            now=moment, older_than_seconds=self.config.recover_after_seconds
        )

        unknown_before = len(self.store.load_unknown())
        reconciliation: UnknownReport | None = None
        if self.config.reconcile_on_unknown and unknown_before:
            reconciliation = reconcile_unknown(
                store=self.store, lookup=self.lookup, now=moment
            )
        unknown_after = len(self.store.load_unknown())

        # Dispatch is gated on the book's fate being fully known: a halted desk does not
        # trade, and an UNKNOWN send means the true position count is unknowable, so a new
        # entry could oversize against a fill that already happened. Reconciliation runs
        # either way - it resolves, it never sends - and a tick that resolves the last
        # unknown dispatches again on the same pass.
        if halted:
            report = DispatchReport()
            report.items.append(
                Dispatched(0, None, "HALTED", halt_reason or "desk halted")
            )
        elif unknown_after:
            report = DispatchReport()
            report.items.append(
                Dispatched(0, None, "UNKNOWN_PENDING",
                           f"{unknown_after} sends of unknown fate; no new orders")
            )
        else:
            report = self.dispatcher.dispatch_once(
                state=state, now=moment, quotes=self.quotes, references=self.references
            )

        live = liveness(
            self.store,
            now=moment,
            stale_after=timedelta(seconds=self.config.stale_after_seconds),
        )
        blocked_by_circuit = any(i.outcome == "CIRCUIT_OPEN" for i in report.items)

        # The tick's own health, not the ingest side's: log staleness is the supervisor's
        # job (`scorpion supervision`), and a quiet market must not page every tick.
        problems: list[str] = []
        if halted:
            problems.append(f"halted: {halt_reason or 'no reason recorded'}")
        if unknown_after:
            problems.append(f"unknown_sends: {unknown_after} awaiting reconciliation")
        if blocked_by_circuit:
            problems.append("circuit_open: venue transport failing")

        return TickReport(
            now=moment,
            dispatched=report,
            reconciliation=reconciliation,
            halted=halted,
            halt_reason=halt_reason,
            unknown_before=unknown_before,
            unknown_after=unknown_after,
            liveness=live,
            circuit_open=self.dispatcher.breaker.is_open,
            problems=tuple(problems),
        )

    def step(self, *, now: datetime | None = None) -> TickReport:
        report = self.tick(now=now)
        if self.series.path is not None:
            self.series.append(report.to_series())
        self.store.heartbeat(
            "service",
            status="running", tick=self._tick_index, exit_code=report.exit_code,
            sent=report.dispatched.sent, unknown=report.unknown_after,
            halted=report.halted,
        )
        self._tick_index += 1
        return report

    def _settle(self, item: Dispatched) -> None:
        """Book a fill the instant a send succeeds, so the next effect sees the truth."""
        if item.outcome != "SENT" or item.envelope is None or item.receipt is None:
            return
        envelope = item.envelope
        receipt = item.receipt
        is_buy = envelope.side == "BUY"
        market_day = self._market_day or datetime.now(UTC).astimezone(MARKET_TZ).date()
        self.pipeline.settle(
            contract_key=envelope.contract_key,
            generation=envelope.generation,
            quantity_delta=receipt.fill_qty if is_buy else -receipt.fill_qty,
            fill_price=receipt.fill_price or envelope.limit_price,
            market_day=market_day,
            final=not is_buy and envelope.effect_kind.value == "PROPOSE_CLOSE",
        )

    # ── the loop ────────────────────────────────────────────────────────────
    def next_delay(self, active: bool) -> float:
        """Cadence with jitter when work happened; exponential backoff when it did not."""
        if active:
            self._idle_streak = 0
            base = self.config.cadence_seconds
        else:
            self._idle_streak += 1
            growth = self.config.idle_backoff_seconds * (2 ** (self._idle_streak - 1))
            base = min(growth, self.config.max_backoff_seconds)
        jitter = self.rng.uniform(-self.config.jitter_seconds, self.config.jitter_seconds)
        return max(0.0, base + jitter)

    def run(self, *, install_signals: bool = True) -> int:
        if install_signals:
            self._install_signal_handlers()
        last_exit = 0
        ticks = 0
        try:
            while self.config.max_ticks is None or ticks < self.config.max_ticks:
                if self._stop:
                    break
                report = self.step()
                last_exit = report.exit_code
                if self._stop:
                    break
                # A reconciliation that resolved nothing (NoLookup leaves rows UNKNOWN) is
                # idle: without this the loop would spin at full cadence on an unresolvable
                # send forever. Only a real resolution counts as work.
                active = report.dispatched.attempted > 0 or bool(
                    report.reconciliation
                    and (report.reconciliation.sent + report.reconciliation.retried) > 0
                )
                time.sleep(self.next_delay(active))
                ticks += 1
        except KeyboardInterrupt:
            self._stop = True
        finally:
            self.store.heartbeat(
                "service", status="stopped", ticks=ticks, exit_code=last_exit
            )
        return last_exit

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Not the main thread, or the platform has no such signal: the loop still
            # honours max_ticks and KeyboardInterrupt.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, lambda _signum, _frame: self.request_stop())


def run_service(
    store: Store,
    *,
    sender: Sender,
    lookup: OrderLookup | None = None,
    policy: Policy | None = None,
    quotes: Mapping[str, Quote] | None = None,
    references: Mapping[str, Decimal] | None = None,
    config: ServiceConfig | None = None,
    seed: int | None = None,
) -> int:
    """Construct and run the service, returning its exit code. The CLI's entry point."""
    try:
        return Service(
            store, sender=sender, lookup=lookup, policy=policy, quotes=quotes,
            references=references, config=config, seed=seed,
        ).run()
    except SnapshotError:
        raise

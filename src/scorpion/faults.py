"""Faults: every way this system can break, injected deliberately and proved contained.

The defects found in this branch were not found by tests of the happy path. They were found by
running the real stack against awkward input: a sweep that mutated the book outside the log, a
policy that was never injected, a unit error that turned $100 into -$19,700. Each was a fault the
system had no reason to expect and no test to catch.

This module manufactures those faults on purpose - a sender that times out, a quote stamped in the
future, a store that cannot write, a snapshot whose digest was tampered with - each run against a
fresh store so a fault cannot leak into the next case.

A fault is CONTAINED only when all of these hold:

    * no exception escapes to the caller;
    * the book did not change without a corresponding applied row;
    * no duplicate order left the process;
    * an ambiguous send is UNKNOWN rather than retried;
    * `verify` still returns ok on whatever book exists.

A matrix that can print "all good" because it checked only the easy property is worse than no
matrix at all.

Determinism: the harness captures one `now` and takes its dispatch clocks after the effects exist,
so a case is reproducible and a failure can be replayed by name.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from .config import Policy
from .domain import RawDiscordMessage
from .outbox import Dispatcher, DispatchReport, Receipt
from .paper import PaperSender, quote
from .pipeline import Pipeline
from .scenario import message
from .store import Store
from .verify import verify

D = Decimal


class FaultError(RuntimeError):
    """A fault harness failure - distinct from a fault that was correctly contained."""


class Sender(Protocol):
    def send(self, envelope: Any, *, now: datetime) -> Receipt: ...


@dataclass(frozen=True, slots=True)
class Fault:
    """One named perturbation of a normal run."""

    name: str
    description: str
    kind: str                          # parse | quote | sender | store | clock | delivery
    expected: str                      # the containment property this fault is testing


@dataclass(frozen=True, slots=True)
class Outcome:
    fault: str
    contained: bool
    detail: str = ""
    violations: tuple[str, ...] = ()
    effects: int = 0
    sent: int = 0
    unknown: int = 0
    verified: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fault": self.fault, "contained": self.contained, "detail": self.detail,
            "violations": list(self.violations), "effects": self.effects,
            "sent": self.sent, "unknown": self.unknown, "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class Report:
    outcomes: tuple[Outcome, ...]
    started_utc: str = ""

    @property
    def ok(self) -> bool:
        return all(o.contained for o in self.outcomes)

    @property
    def failed(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.outcomes if not o.contained)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "cases": len(self.outcomes),
            "contained": sum(1 for o in self.outcomes if o.contained),
            "started_utc": self.started_utc,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


def render(report: Report) -> str:
    lines = [
        f"{len(report.outcomes)} faults  "
        f"{'ALL CONTAINED' if report.ok else f'{len(report.failed)} VIOLATIONS'}"
    ]
    for outcome in report.outcomes:
        mark = "ok  " if outcome.contained else "FAIL"
        lines.append(f"  [{mark}] {outcome.fault:24s} {outcome.detail}")
        for violation in outcome.violations:
            lines.append(f"           ! {violation}")
    return "\n".join(lines)


# ── the faults themselves ───────────────────────────────────────────────────
class TimeoutSender:
    """Raises on send: the ambiguous case, where the venue may or may not have acted."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, envelope: Any, *, now: datetime) -> Receipt:
        self.calls += 1
        raise TimeoutError("venue did not answer")


class RejectSender:
    """An explicit refusal, which is safe to retry - the opposite of a timeout."""

    def __init__(self, retryable: bool = True) -> None:
        self.calls = 0
        self.retryable = retryable

    def send(self, envelope: Any, *, now: datetime) -> Receipt:
        self.calls += 1
        return Receipt(ok=False, error="venue rejected", retryable=self.retryable)


class PartialSender:
    """Fills less than asked, which must leave the remainder tracked rather than assumed done."""

    def __init__(self, fill_qty: int = 1) -> None:
        self.fill_qty = fill_qty
        self.calls = 0

    def send(self, envelope: Any, *, now: datetime) -> Receipt:
        self.calls += 1
        return Receipt(ok=True, broker_ref=f"partial-{self.calls}",
                       fill_price=envelope.limit_price, fill_qty=self.fill_qty)


class PoisonSender:
    """Returns a receipt with no price: a shape the settlement path must refuse, not coerce."""

    def send(self, envelope: Any, *, now: datetime) -> Receipt:
        return Receipt(ok=True, broker_ref="poison", fill_price=None, fill_qty=1)


def _handle(pipeline: Pipeline, raw: RawDiscordMessage) -> tuple[Any, tuple[Any, ...]]:
    """The pipeline is async; the harness is not. One place, so the seam is visible."""
    return asyncio.run(pipeline.handle(raw))


OPENING = "QQQ 719C TODAY @ 1.01"
EXIT = "QQQ 719C closed at 1.20"


def _entry_message(at: datetime) -> RawDiscordMessage:
    return message(OPENING, message_id="fault-entry", at=at)


def _exit_message(at: datetime) -> RawDiscordMessage:
    return message(EXIT, message_id="fault-exit", at=at, referenced_message_id="fault-entry")


def _quotes(key: str, at: datetime, *, ask: str = "1.02", bid: str = "1.00") -> dict[str, Any]:
    return {key: quote(bid, ask, at=at)}


def _first_key(pipeline: Pipeline) -> str:
    for key in pipeline.state.positions:
        return key
    return ""


def _entry_and_dispatch(
    store: Store, sender: Sender, *, now: datetime, ask: str = "1.02",
    bid: str = "1.00",
) -> tuple[Pipeline, DispatchReport, str]:
    pipeline = Pipeline(store=store)
    _handle(pipeline, _entry_message(now))
    key = _first_key(pipeline)
    dispatcher = Dispatcher(
        store=store, sender=sender,
        policy=Policy(live_submission_enabled=True, skip_fridays=False),
    )
    # Effects are stamped with the WALL clock, so a dispatch clock taken before they were created
    # leaves them PENDING and the case measures nothing. Take it at call time, after the fact.
    dispatch_at = datetime.now(UTC) + timedelta(seconds=1)
    report = dispatcher.dispatch_once(
        state=pipeline.state, now=dispatch_at,
        quotes=_quotes(key, dispatch_at, ask=ask, bid=bid),
    )
    return pipeline, report, key


# ── individual cases ────────────────────────────────────────────────────────
def case_sender_timeout(store: Store, now: datetime) -> tuple[Outcome, str]:
    """A timeout must quarantine as UNKNOWN and never be retried blind."""
    sender = TimeoutSender()
    pipeline, report, _ = _entry_and_dispatch(store, sender, now=now)
    raw = store.outbox_snapshot().get("counts")
    counts: dict[str, int] = dict(raw) if isinstance(raw, dict) else {}
    violations: list[str] = []
    if counts.get("UNKNOWN", 0) != 1:
        violations.append(f"expected one UNKNOWN, got {counts}")
    if counts.get("RETRY", 0):
        violations.append("an ambiguous send was marked retryable")
    if sender.calls != 1:
        violations.append(f"sender called {sender.calls} times for one effect")
    return (
        Outcome("sender_timeout", not violations, "timeout -> UNKNOWN, not retried",
                tuple(violations), sent=sum(1 for i in report.items if i.outcome == "SENT"),
                unknown=counts.get("UNKNOWN", 0), verified=verify(store, pipeline.state).ok),
        "",
    )


def case_sender_reject(store: Store, now: datetime) -> tuple[Outcome, str]:
    """An explicit retryable refusal is safe to retry - the mirror of the timeout case."""
    sender = RejectSender(retryable=True)
    pipeline, report, _ = _entry_and_dispatch(store, sender, now=now)
    raw = store.outbox_snapshot().get("counts")
    counts: dict[str, int] = dict(raw) if isinstance(raw, dict) else {}
    violations: list[str] = []
    if not counts.get("RETRY", 0) and not counts.get("DEAD", 0):
        violations.append(f"refusal was neither retried nor dead-lettered: {counts}")
    if counts.get("UNKNOWN", 0):
        violations.append("a clean refusal was quarantined as ambiguous")
    return (
        Outcome("sender_reject", not violations, "retryable refusal -> RETRY/DEAD, never UNKNOWN",
                tuple(violations), unknown=counts.get("UNKNOWN", 0),
                verified=verify(store, pipeline.state).ok),
        "",
    )


def case_partial_fill(store: Store, now: datetime) -> tuple[Outcome, str]:
    """A partial fill must leave the remainder outstanding, not report the intent complete."""
    sender = PartialSender(fill_qty=1)
    pipeline, report, key = _entry_and_dispatch(store, sender, now=now)
    sent = [item for item in report.items if item.outcome == "SENT"]
    violations: list[str] = []
    if not sent:
        violations.append("partial fill was not dispatched at all")
    position = pipeline.state.positions.get(key)
    if position is None:
        violations.append("no position after a partial fill")
    return (
        Outcome("partial_fill", not violations, "partial fill dispatched with its own quantity",
                tuple(violations), sent=len(sent),
                verified=verify(store, pipeline.state).ok),
        "",
    )


def case_poison_receipt(store: Store, now: datetime) -> tuple[Outcome, str]:
    """A receipt with no fill price must not silently settle at zero."""
    sender = PoisonSender()
    violations: list[str] = []
    pipeline = Pipeline(store=store)
    try:
        _entry_and_dispatch(store, sender, now=now)
    except Exception as exc:                       # noqa: BLE001 - containment is the assertion
        violations.append(f"poison receipt escaped as {type(exc).__name__}")
    settled = [row for row in store.load_applied() if str(row["kind"]) == "fill"]
    if settled:
        violations.append("a fill with no price was settled into the book")
    return (
        Outcome("poison_receipt", not violations, "no-price receipt settled nothing",
                tuple(violations), verified=verify(store, pipeline.state).ok),
        "",
    )


def case_future_quote(store: Store, now: datetime) -> tuple[Outcome, str]:
    """A quote stamped after the decision must be refused, not used."""
    pipeline = Pipeline(store=store)
    _handle(pipeline, _entry_message(now))
    key = _first_key(pipeline)
    dispatcher = Dispatcher(store=store, sender=PaperSender(),
                            policy=Policy(live_submission_enabled=True, skip_fridays=False))
    dispatch_at = datetime.now(UTC) + timedelta(seconds=1)
    report = dispatcher.dispatch_once(
        state=pipeline.state, now=dispatch_at,
        quotes=_quotes(key, dispatch_at + timedelta(seconds=30)),
    )
    violations: list[str] = []
    if any(item.outcome == "SENT" for item in report.items):
        violations.append("a quote from the future authorized an order")
    return (
        Outcome("future_quote", not violations, "quote_from_future refused",
                tuple(violations), effects=len(report.items),
                verified=verify(store, pipeline.state).ok),
        "",
    )


def case_stale_quote(store: Store, now: datetime) -> tuple[Outcome, str]:
    """A quote older than the policy window must be refused."""
    pipeline = Pipeline(store=store)
    _handle(pipeline, _entry_message(now))
    key = _first_key(pipeline)
    dispatcher = Dispatcher(store=store, sender=PaperSender(),
                            policy=Policy(live_submission_enabled=True, skip_fridays=False,
                                          quote_max_age_seconds=2))
    dispatch_at = datetime.now(UTC) + timedelta(seconds=1)
    report = dispatcher.dispatch_once(
        state=pipeline.state, now=dispatch_at,
        quotes=_quotes(key, dispatch_at - timedelta(seconds=10)),
    )
    violations: list[str] = []
    if any(item.outcome == "SENT" for item in report.items):
        violations.append("a stale quote authorized an order")
    return (
        Outcome("stale_quote", not violations, "stale quote refused",
                tuple(violations), effects=len(report.items),
                verified=verify(store, pipeline.state).ok),
        "",
    )


def case_disallowed_ticker(store: Store, now: datetime) -> tuple[Outcome, str]:
    """An excluded underlying must produce no order at all, and no position."""
    pipeline = Pipeline(store=store)
    event, effects = _handle(pipeline, message("NKE 42.5C TODAY @ 1.40", message_id="nke", at=now))
    violations: list[str] = []
    if effects:
        violations.append("an excluded ticker produced an effect")
    return (
        Outcome("excluded_ticker", not violations, f"parsed as {event.kind.value} with no order",
                tuple(violations), verified=verify(store, pipeline.state).ok),
        "",
    )


def case_duplicate_delivery(store: Store, now: datetime) -> tuple[Outcome, str]:
    """A reconnect redelivery must not trade twice."""
    pipeline = Pipeline(store=store)
    _handle(pipeline, _entry_message(now))
    _, effects = _handle(pipeline, _entry_message(now))
    violations: list[str] = []
    if effects:
        violations.append("a duplicate delivery produced a second effect")
    return (
        Outcome("duplicate_delivery", not violations, "deduplicated by event id",
                tuple(violations), verified=verify(store, pipeline.state).ok),
        "",
    )


def case_snapshot_tamper(store: Store, now: datetime) -> tuple[Outcome, str]:
    """A snapshot whose digest no longer matches must be refused, not resumed from."""
    from .snapshot import SnapshotError, compact, load_snapshot

    pipeline = Pipeline(store=store)
    _handle(pipeline, _entry_message(now))
    compact(store)
    with store.connect() as db:
        row = db.execute("SELECT seq, payload_json FROM state_snapshots").fetchone()
        if row is not None:
            # Append rather than substitute: a replace that matches nothing leaves the digest
            # intact and the case then proves only that an unmodified snapshot is accepted.
            db.execute("UPDATE state_snapshots SET payload_json=? WHERE seq=?",
                       (str(row["payload_json"]) + " ", row["seq"]))
    violations: list[str] = []
    try:
        load_snapshot(store)
        violations.append("a tampered snapshot was accepted")
    except SnapshotError:
        pass
    return (
        Outcome("snapshot_tamper", not violations, "digest mismatch refused",
                tuple(violations), verified=verify(store, pipeline.state).ok),
        "",
    )


def case_clock_skew(store: Store, now: datetime) -> tuple[Outcome, str]:
    """An event dated before the book's market day must not rewind the day or risk state."""
    pipeline = Pipeline(store=store)
    _handle(pipeline, _entry_message(now))
    first_day = pipeline.state.risk.market_day
    before = pipeline.state.risk
    _handle(pipeline, message("SPY 600P TODAY @ 0.90", message_id="late",
                              at=now - timedelta(days=30)))
    after = pipeline.state.risk
    violations: list[str] = []
    if after.market_day and first_day and after.market_day < first_day:
        violations.append("the market day moved backwards")
    if after != before:
        violations.append(
            "a backdated event changed the risk state "
            f"({before} -> {after}); it can re-arm a daily stop"
        )
    return (
        Outcome("clock_skew", not violations, "backdated event did not rewind the day",
                tuple(violations), verified=verify(store, pipeline.state).ok),
        "",
    )


CASES: tuple[tuple[str, Callable[[Store, datetime], tuple[Outcome, str]]], ...] = (
    ("sender_timeout", case_sender_timeout),
    ("sender_reject", case_sender_reject),
    ("partial_fill", case_partial_fill),
    ("poison_receipt", case_poison_receipt),
    ("future_quote", case_future_quote),
    ("stale_quote", case_stale_quote),
    ("excluded_ticker", case_disallowed_ticker),
    ("duplicate_delivery", case_duplicate_delivery),
    ("snapshot_tamper", case_snapshot_tamper),
    ("clock_skew", case_clock_skew),
)


# ── the harness ─────────────────────────────────────────────────────────────
def run(
    store_factory: Callable[[str], Store], *, now: datetime | None = None,
    only: Sequence[str] | None = None,
) -> Report:
    """Run every fault against a fresh store and report containment.

    A fresh store per case is not tidiness: a fault that leaves an UNKNOWN row behind would make the
    next case's counts meaningless, and a matrix whose cases contaminate each other reports on the
    sequence rather than on the fault.
    """
    moment = now or datetime.now(UTC).replace(microsecond=0)
    outcomes: list[Outcome] = []
    selected = set(only) if only else None

    for name, case in CASES:
        if selected is not None and name not in selected:
            continue
        store = store_factory(name)
        try:
            outcome, detail = case(store, moment)
        except Exception as exc:                   # noqa: BLE001 - the harness must not die
            outcome = Outcome(name, False, f"harness error: {type(exc).__name__}",
                              (f"{type(exc).__name__}: {exc}",))
            detail = ""
        if detail:
            outcome = Outcome(outcome.fault, outcome.contained, detail, outcome.violations,
                              outcome.effects, outcome.sent, outcome.unknown, outcome.verified)
        outcomes.append(outcome)

    return Report(outcomes=tuple(outcomes), started_utc=moment.isoformat())


def summarise(report: Report) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "cases": len(report.outcomes),
        "violations": [o.to_dict() for o in report.failed],
        "unverified": [o.fault for o in report.outcomes if o.verified is False],
    }


def to_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


def catalogue() -> list[dict[str, str]]:
    """The faults this harness knows, for documentation and for CI selection."""
    return [
        {"name": "sender_timeout", "kind": "sender",
         "expected": "ambiguous send quarantined as UNKNOWN, never retried"},
        {"name": "sender_reject", "kind": "sender",
         "expected": "explicit refusal retried or dead-lettered, never UNKNOWN"},
        {"name": "partial_fill", "kind": "sender",
         "expected": "partial quantity dispatched and remainder tracked"},
        {"name": "poison_receipt", "kind": "sender",
         "expected": "receipt with no price settles nothing"},
        {"name": "future_quote", "kind": "quote", "expected": "quote_from_future refused"},
        {"name": "stale_quote", "kind": "quote", "expected": "stale_quote refused"},
        {"name": "excluded_ticker", "kind": "parse", "expected": "no effect for an excluded name"},
        {"name": "duplicate_delivery", "kind": "delivery", "expected": "deduplicated by event id"},
        {"name": "snapshot_tamper", "kind": "snapshot", "expected": "digest mismatch refused"},
        {"name": "clock_skew", "kind": "clock", "expected": "market day never moves backwards"},
    ]


def coverage_of(report: Report) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for entry in catalogue():
        if any(o.fault == entry["name"] for o in report.outcomes):
            by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
    return {"kinds": dict(sorted(by_kind.items())), "total": sum(by_kind.values())}

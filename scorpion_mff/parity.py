"""Shadow-live parity: prove the *wiring* does not change a decision.

The live lane has two harnesses that look similar and answer different questions.

* `replay.py` asks **"what would the executor have done with the corpus?"** It runs the
  real parser, engine, pricing and sizing over a recorded tape and reports the arithmetic:
  4 events, 2 buys, 6 contracts, $211.00. That number is pinned, and it is the regression
  oracle for every rule in `live/RULES.lock.md`.
* This module asks **"does the live wiring change what the executor decided?"**

They are not the same question, and conflating them is how a lane ends up "proving" a
safety feature by showing that its own replay still passes. `replay` deliberately runs the
smallest possible stack: a tape, a controller, a paper broker. The live process is larger.
Between the decision and the send it now carries a confirmed-execution book
(`execution_book.py`), an in-flight exit guard, a realised-P&L alignment, a circuit breaker
and a scheduled reconcile. Every one of those is a chance to change a decision without
changing a test.

So this harness runs **the same events through two wirings**:

    reference = tape -> parser -> engine -> PaperBroker                (the decision)
    candidate = tape -> parser -> engine -> ShadowBroker -> ExecBook   (the live ladder)

and compares, event by event, in order:

* the intents the engine emitted (action, quantity, limit, contract, reason);
* every decision row those intents produced (`buy`, `sell`, `intent`, `ack`, `skip`,
  `reject`, `transport_unknown`) with its reason and idempotency key;
* the engine's state *after* the event: positions, realised P&L, trades today, closed
  contracts, the halt flag and the in-flight exit set.

A difference is a **divergence**, and a divergence is the product: it names the event, the
field, both values and the most likely live-only cause. It never repairs, never smooths and
never exits 0 because "the totals still match". Cash equality is not decision equality -
two wirings can realise the same dollars by entering different contracts.

Things this harness will not do
-------------------------------
* It never sends an order. `ShadowBroker` is a deterministic venue model, not a client: it
  has no URL, no token, no transport and no retry. It exists to answer `place()` so the rest
  of the ladder (intent row, breaker, confirmed book, cumulative ack, P&L alignment) runs.
* It never ticks a clock. Ticks are *events created by time*, not by a post, so they have no
  counterpart in a tape. Parity over events is a closed question; measuring the clock is
  `drift.py`'s job and the cutoff is `expiry.py`'s. Mixing them here would let a tick-driven
  expiry flatten excuse a divergence somewhere else.
* It never compares wall-clock timestamps. The tape's absolute time is an arbitrary ordered
  base, so both wirings are driven by one scripted clock that maps the first message onto a
  real trading session. Timing is measured, never asserted as equality.

The record is a JSON trace (`TRACE_SCHEMA = 1`), so a **real** shadow session - `arm
--trace live/shadow.jsonl` - can be diffed later against the offline replay of the same
tape with `python -m scorpion_mff.cli shadow --diff live/shadow.json tapes/sep8.json`:

    python -m scorpion_mff.cli shadow tapes/sep8.json          # parity: exit 0 / 1 / 2
    python -m scorpion_mff.cli shadow tapes/sep8.json --json   # machine-readable
    python -m scorpion_mff.cli shadow --diff A.json B.json      # two recorded traces
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .broker import (
    Ack,
    BrokerError,
    OrderLookup,
    PaperBroker,
    QuoteScript,
    TransportError,
    intent_key,
)
from .engine import Engine, EngineConfig
from .execution_book import ExecutionBook
from .fills import FillLog
from .health import Heartbeat, LatencyBudget
from .killswitch import KillSwitch
from .types import OrderIntent, RawMessage, Signal

TRACE_SCHEMA = 1

MODES: tuple[str, ...] = ("replay", "shadow")

#: The rows that describe a decision. `reconcile` and `alert` rows are bookkeeping and
#: diagnostics: they are captured for the report but never compared, because the shadow
#: side legitimately writes more of them (the confirmed book is live-only machinery).
DECISION_EVENTS = frozenset({
    "buy", "sell", "skip", "reject", "halt", "intent", "ack",
    "transport_unknown", "recovered", "not_sent",
})
#: The rows a human must be shown when a divergence needs a cause. Kept separate so an
#: engine alert is never mistaken for a decision.
DIAGNOSTIC_EVENTS = frozenset({"alert", "reconcile"})

#: 2026-09-08 13:30:00 UTC = 09:30 ET, the first session the stack has a post-mortem for.
#: Only the *origin* is chosen: the tape's relative spacing is preserved exactly, so two
#: messages a hundred seconds apart stay a hundred seconds apart.
DEFAULT_BASE_TS = datetime(2026, 9, 8, 13, 30, tzinfo=UTC).timestamp()

#: Fields compared per event, in the order they are reported. Intents first because an
#: intent difference explains a state difference, not the other way round.
COMPARED_FIELDS: tuple[str, ...] = ("kind", "contract_key", "intents", "decisions", "state")


# ── the shadow venue ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VenuePolicy:
    """How the shadow venue answers one `place()`. Deterministic, by construction.

    The default is a full fill at the submitted limit, which is exactly `PaperBroker`'s
    behaviour - so the default shadow run isolates the *wiring* and nothing else. The
    other knobs exist so a test can force the ladder into the states a live venue will
    eventually produce and prove the harness *catches* the divergence rather than hiding
    it:

    * `fill_fraction < 1` - a partial ack, which is where the confirmed book and the
      engine's decision cache are allowed to disagree (a partial **sell** must restore the
      remainder to the engine and keep it in flight);
    * `reject` - a business refusal, terminal, which must free the engine's slot;
    * `transport` - the socket died and the venue may or may not have the order, which must
      stay `unknown` and must never become a terminal.

    A policy that is both a reject and a transport failure is refused: those are different
    messages from the venue and only one of them can be true.
    """

    fill_fraction: float = 1.0
    price_offset: float = 0.0
    reject: str = ""
    transport: str = ""

    def __post_init__(self) -> None:
        if not 0.0 < self.fill_fraction <= 1.0:
            raise ValueError(
                f"fill_fraction must be inside (0, 1], not {self.fill_fraction!r}")
        if self.reject and self.transport:
            raise ValueError(
                "a venue answer cannot be both a reject and a transport failure")
        if not math.isfinite(self.price_offset):
            raise ValueError("price_offset must be finite")

    @property
    def label(self) -> str:
        """A one-line description for the parity header; never contains a secret."""
        if self.transport:
            return f"transport:{self.transport}"
        if self.reject:
            return f"reject:{self.reject}"
        if self.fill_fraction < 1.0 or self.price_offset:
            return f"fill={self.fill_fraction:g}:offset={self.price_offset:+.2f}"
        return "fill@limit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_fraction": self.fill_fraction,
            "price_offset": self.price_offset,
            "reject": self.reject,
            "transport": self.transport,
            "label": self.label,
        }


@dataclass
class ShadowBroker:
    """A venue that answers and never sends.

    The live listener's post-decision ladder is not a formality: it writes the intent
    before the send, consults the breaker for buys only, folds a cumulative ack into the
    confirmed book, restores a partial sell to the engine, and re-bases realised P&L onto
    the confirmed quantity. All of that has to run for shadow mode to mean anything - so
    it runs here, against a model instead of a socket.

    There is deliberately **no** `base_url`, `token`, `transport`, `retries` or `timeout`
    field. A shadow broker that can be pointed at a venue is a live broker with a flag, and
    the one thing this class must be able to prove is that it cannot send anything.
    """

    policy: VenuePolicy = field(default_factory=VenuePolicy)
    acks: list[Ack] = field(default_factory=list)
    sent: list[OrderIntent] = field(default_factory=list)
    placed_keys: set[str] = field(default_factory=set)

    def place(self, intent: OrderIntent) -> Ack:
        """Answer as the configured venue would. Never an I/O call, never a retry."""
        if intent.qty <= 0:
            # The same refusal PaperBroker and the HTTP adapter make: a zero or negative
            # clip is a sizing bug, not an order.
            raise BrokerError(f"refusing a {intent.qty}-contract order")
        if intent.action not in ("buy", "sell"):
            raise BrokerError(f"unknown action {intent.action!r}")
        self.sent.append(intent)
        policy = self.policy
        if policy.transport:
            # The socket died. Whether the venue has the order is now unknown, which is
            # exactly the state `transport_unknown` exists to preserve.
            raise TransportError(f"{policy.transport}: shadow venue did not answer")
        if policy.reject:
            raise BrokerError(policy.reject)
        qty = (intent.qty if policy.fill_fraction >= 1.0
               else max(1, math.floor(intent.qty * policy.fill_fraction)))
        price = round(max(0.01, intent.limit + policy.price_offset), 4)
        ack = Ack(ok=True, contract_key=intent.contract_key, qty=qty, price=price,
                  broker_order_id=f"shadow-{len(self.acks) + 1}")
        self.acks.append(ack)
        self.placed_keys.add(intent_key(intent))
        return ack

    def lookup(self, idempotency_key: str) -> OrderLookup:
        """Deterministic tri-state: this model either saw the key or it did not."""
        return (OrderLookup.SEEN if idempotency_key in self.placed_keys
                else OrderLookup.NOT_SEEN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "sent": len(self.sent),
            "acked": len(self.acks),
            "filled_contracts": sum(a.qty for a in self.acks),
        }


# ── the trace ────────────────────────────────────────────────────────────────
def state_of(engine: Engine) -> str:
    """The engine's book after one event, canonicalised for equality.

    Everything here is decision-relevant. `realized_usd` is rounded to cents because the
    engine already rounds money to cents on its own path; comparing full float noise would
    report a divergence for a rounding artefact that no rule can act on. Contracts and
    average price are compared exactly (they are integers and limits).
    """
    positions = ";".join(
        f"{key}={position.contracts}@{position.avg_price:.6f}"
        for key, position in sorted(engine.positions.items())
    )
    return (
        f"pos[{positions}]"
        f" realized={engine.stats.realized_usd:.2f}"
        f" trades={engine.stats.trades_today}"
        f" closed[{','.join(sorted(engine.closed))}]"
        f" halted={int(engine.halted)}:{engine.halt_reason}"
        f" inflight[{','.join(sorted(engine.exit_in_flight))}]"
    )


def _intent_row(intent: OrderIntent) -> str:
    """One intent, canonicalised. The reason is part of the identity, not a comment."""
    return (f"{intent.action}|{intent.qty}|{intent.limit:g}|"
            f"{intent.contract_key}|{intent.reason}|{intent.idempotency_key}")


def _fill_row(fill: Any) -> str:
    """One decision row, canonicalised. `latency_ms` is deliberately excluded."""
    return (f"{fill.event}|{fill.contract_key}|{fill.qty}|{fill.price:g}|"
            f"{fill.reason}|{fill.accepted}|{fill.note}")


@dataclass(frozen=True)
class EventTrace:
    """One message's whole contribution to the book: what was decided, and what remains."""

    index: int
    message_id: str
    channel: str
    kind: str
    contract_key: str
    intents: tuple[str, ...]
    decisions: tuple[str, ...]
    state: str
    diagnostics: tuple[str, ...] = ()

    def field(self, name: str) -> str:
        if name not in COMPARED_FIELDS:
            raise KeyError(f"{name!r} is not a compared field; expected {COMPARED_FIELDS}")
        value: Any = getattr(self, name)
        return value if isinstance(value, str) else "; ".join(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "message_id": self.message_id,
            "channel": self.channel,
            "kind": self.kind,
            "contract_key": self.contract_key,
            "intents": list(self.intents),
            "decisions": list(self.decisions),
            "state": self.state,
            "diagnostics": list(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EventTrace:
        def rows(name: str) -> tuple[str, ...]:
            value = payload.get(name, ())
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ValueError(f"trace event {name!r} is not a list")
            return tuple(str(item) for item in value)

        return cls(
            index=int(payload["index"]),
            message_id=str(payload.get("message_id", "")),
            channel=str(payload.get("channel", "")),
            kind=str(payload.get("kind", "")),
            contract_key=str(payload.get("contract_key", "")),
            intents=rows("intents"),
            decisions=rows("decisions"),
            state=str(payload.get("state", "")),
            diagnostics=rows("diagnostics"),
        )


@dataclass
class Trace:
    """An ordered, serialisable record of what one wiring did with a stream of messages.

    `record()` is called by `Listener.handle` when a trace is attached, which is the only
    hook the live path needs. That matters: a parity harness that observes the live path
    through a second code path proves nothing about the live path. This one reads the same
    objects the process just produced - the returned intents, the fill-log rows appended
    during the event, and the engine's own book.
    """

    mode: str = "shadow"
    tape: str = ""
    schema: int = TRACE_SCHEMA
    pinned: dict[str, Any] = field(default_factory=dict)
    events: list[EventTrace] = field(default_factory=list)
    # The fill-log cursor: rows are captured per event so a decision cannot be attributed
    # to the wrong message by sequence number alone.
    _cursor: int = 0

    def record(self, *, message: RawMessage, signal: Signal,
               intents: Iterable[OrderIntent], engine: Engine,
               rows: Sequence[Any]) -> EventTrace:
        """Capture one handled message. Called by `Listener.handle`; never raises."""
        fresh = list(rows[self._cursor:])
        self._cursor = len(rows)
        decisions = tuple(_fill_row(row) for row in fresh
                          if row.event in DECISION_EVENTS)
        diagnostics = tuple(_fill_row(row) for row in fresh
                            if row.event in DIAGNOSTIC_EVENTS)
        event = EventTrace(
            index=len(self.events),
            message_id=message.message_id,
            channel=message.channel,
            kind=str(signal.kind.value),
            contract_key=signal.contract_key,
            intents=tuple(_intent_row(intent) for intent in intents),
            decisions=decisions,
            state=state_of(engine),
            diagnostics=diagnostics,
        )
        self.events.append(event)
        return event

    # ── serialisation ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "mode": self.mode,
            "tape": self.tape,
            "pinned": dict(self.pinned),
            "events": [event.to_dict() for event in self.events],
        }

    def save(self, path: str | os.PathLike[str]) -> Path:
        """Write the trace atomically: a half-written trace is a false clean run."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = target.with_suffix(target.suffix + ".tmp")
        scratch.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
        scratch.replace(target)
        return target

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Trace:
        """Read a trace file. Anything that is not this schema is refused, not guessed."""
        target = Path(path)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"cannot read trace {target}: {exc}") from None
        except json.JSONDecodeError as exc:
            raise ValueError(f"trace {target} is not JSON: {exc}") from None
        if not isinstance(payload, Mapping):
            raise ValueError(f"trace {target} is not an object")
        version = payload.get("schema")
        if version != TRACE_SCHEMA:
            raise ValueError(
                f"trace {target} declares schema {version!r}; this build reads v{TRACE_SCHEMA}")
        raw_events = payload.get("events")
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise ValueError(f"trace {target} has no event list")
        trace = cls(mode=str(payload.get("mode", "")), tape=str(payload.get("tape", "")),
                    pinned=dict(payload.get("pinned") or {}))
        for item in raw_events:
            if not isinstance(item, Mapping):
                raise ValueError(f"trace {target} has a non-object event")
            trace.events.append(EventTrace.from_dict(item))
        return trace

    # ── facts ────────────────────────────────────────────────────────────────
    def summary(self) -> dict[str, Any]:
        """Counts read from the decisions themselves, so both sides are comparable."""
        events = self.events
        decisions = [row for event in events for row in event.decisions]
        return {
            "mode": self.mode,
            "events": len(events),
            "buys": sum(1 for row in decisions if row.startswith("buy|")),
            "sells": sum(1 for row in decisions if row.startswith("sell|")),
            "intents": sum(len(event.intents) for event in events),
            "skips": sum(1 for row in decisions if row.startswith("skip|")),
            "rejects": sum(1 for row in decisions if row.startswith("reject|")),
            "alerts": sum(1 for row in decisions
                          if row.startswith("transport_unknown|")),
            "digest": _trace_digest(events),
        }


def _trace_digest(events: Sequence[EventTrace]) -> str:
    """A short content digest: two traces are the same book if this matches.

    Same discipline as `execution_book.digest`: identity is content, not a counter or a
    timestamp, so a trace recorded today can be compared with one recorded last week.
    """
    import hashlib

    canonical = json.dumps([event.to_dict() for event in events],
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── the comparison ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Divergence:
    """One event, one field, two values, and the best available cause."""

    index: int
    field: str
    reference: str
    candidate: str
    cause: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "field": self.field,
            "reference": self.reference, "candidate": self.candidate,
            "cause": self.cause,
        }


def _cause(reference: EventTrace, candidate: EventTrace, field: str) -> str:
    """Name the most likely live-only mechanism behind a divergence.

    The rules are deliberately conservative and textual: they only name a cause when the
    record itself contains the evidence (`transport_unknown`, a `broker:` reject, an
    `ack` whose cumulative quantity moved, an engine alert, an in-flight set). Anything
    else is reported as `unexplained`, because a plausible-sounding cause with no row
    behind it is worse than an honest blank - it is how a real defect gets archived as a
    known effect.
    """
    haystack = " ".join(candidate.decisions + candidate.diagnostics)
    if "transport_unknown|" in haystack:
        return "shadow venue did not answer (transport model)"
    if "|broker:" in haystack:
        return "shadow venue refused the order (reject model)"
    if field == "decisions" and _has_row(reference.decisions, "ack") \
            and _has_row(candidate.decisions, "ack"):
        # Both sides acknowledged; the difference is the answer itself, not the ladder.
        return "venue answer differs (cumulative ack quantity or price)"
    if candidate.diagnostics:
        first = candidate.diagnostics[0].split("|", 4)
        if len(first) >= 5:
            return f"diagnostic row: {first[4]}"
    if field == "state":
        if "inflight[" in candidate.state and candidate.state != reference.state:
            return "confirmed-book effect (in-flight exit set)"
        return "confirmed-book effect"
    if field == "intents":
        return "decision changed before the venue answered"
    if field == "decisions":
        return "decision row changed after the venue answered"
    return "unexplained"


def _abbrev(value: str, limit: int = 180) -> str:
    """A report line a human can scan. The comparison itself is never abbreviated."""
    if not value:
        return "(empty)"
    return value if len(value) <= limit else value[: limit - 1] + "\u2026"


#: Causes that describe *this* row versus causes that describe the wiring's past. A weak
#: cause is qualified with the most recent strong cause, because the decision that changed
#: at event 3 is usually a consequence of the venue answer at event 0.
_WEAK_CAUSES = frozenset({
    "decision changed before the venue answered",
    "decision row changed after the venue answered",
    "unexplained",
})


def _has_row(rows: Sequence[str], event: str) -> bool:
    """Whether a decision slice contains a row of this event type."""
    return any(row.startswith(f"{event}|") for row in rows)


@dataclass
class ParityReport:
    """The answer: did the live wiring change a single recorded decision?"""

    reference: Trace
    candidate: Trace
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.divergences

    @property
    def events(self) -> int:
        return max(len(self.reference.events), len(self.candidate.events))

    @property
    def matched(self) -> int:
        return self.events - len({divergence.index for divergence in self.divergences})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TRACE_SCHEMA,
            "ok": self.ok,
            "events": self.events,
            "matched": self.matched,
            "reference": self.reference.summary(),
            "candidate": self.candidate.summary(),
            "pinned": dict(self.candidate.pinned),
            "divergences": [row.to_dict() for row in self.divergences],
        }

    def to_text(self) -> str:
        reference, candidate = self.reference, self.candidate
        verdict = "PARITY" if self.ok else f"DIVERGED ({len(self.divergences)})"
        lines = [
            f"parity  reference={reference.mode}  candidate={candidate.mode}  "
            f"events={self.events}  matched={self.matched}  {verdict}",
        ]
        for side in (reference, candidate):
            summary = side.summary()
            lines.append(
                f"  {side.mode:<7} events {summary['events']:>3}  buys {summary['buys']:>3}  "
                f"sells {summary['sells']:>3}  skips {summary['skips']:>3}  "
                f"rejects {summary['rejects']:>3}  digest {summary['digest'][:16]}")
        if candidate.pinned:
            policy = candidate.pinned.get("venue", {})
            if policy:
                lines.append(f"  venue   {policy.get('label', '?')}")
        for divergence in self.divergences:
            lines.extend([
                f"  event {divergence.index}  field={divergence.field}",
                f"    reference  {_abbrev(divergence.reference)}",
                f"    candidate  {_abbrev(divergence.candidate)}",
                f"    cause      {divergence.cause or 'unexplained'}",
            ])
        return "\n".join(lines)


def compare(reference: Trace, candidate: Trace) -> ParityReport:
    """Compare two traces event by event, field by field, in a fixed order.

    The comparison is total: it never raises on a malformed event, never stops at the first
    difference (a report with one divergence hides the second, and it is usually the second
    that explains the first), and never treats a shorter trace as a pass. A missing event is
    itself a divergence - losing a message is the 9/8 failure.
    """
    report = ParityReport(reference=reference, candidate=candidate)
    total = max(len(reference.events), len(candidate.events))
    context = ""
    for index in range(total):
        left = reference.events[index] if index < len(reference.events) else None
        right = candidate.events[index] if index < len(candidate.events) else None
        if left is None or right is None:
            report.divergences.append(Divergence(
                index=index, field="events",
                reference="missing" if left is None else left.to_dict().__repr__(),
                candidate="missing" if right is None else right.to_dict().__repr__(),
                cause="one wiring lost or invented an event",
            ))
            continue
        for name in COMPARED_FIELDS:
            expected, actual = left.field(name), right.field(name)
            if expected == actual:
                continue
            cause = _cause(left, right, name)
            if cause in _WEAK_CAUSES and context:
                cause = f"{cause} (after: {context})"
            elif cause not in _WEAK_CAUSES:
                context = cause
            report.divergences.append(Divergence(
                index=index, field=name, reference=expected, candidate=actual,
                cause=cause,
            ))
    return report


# ── the two wirings ──────────────────────────────────────────────────────────
class TapeClock:
    """A clock the harness sets, so both wirings see the same instant per event."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def set(self, now: float) -> None:
        self.now = float(now)


@dataclass
class RunResult:
    trace: Trace
    listener: Any
    broker: Any


def run_tape(tape: Sequence[RawMessage],
             quotes: Mapping[str, Sequence[Sequence[float]]],
             *, mode: str, engine_config: EngineConfig | None = None,
             venue: VenuePolicy | None = None,
             base_ts: float | None = None,
             tape_name: str = "") -> RunResult:
    """Run one tape through one wiring and return its trace.

    `mode="replay"` is the decision: engine + PaperBroker, no confirmed book, no breaker
    bookkeeping beyond the listener default, no reconcile. `mode="shadow"` is the live
    ladder: the same engine and the same events, but every send goes through
    `ShadowBroker` and every answer is folded into an `ExecutionBook` with the same
    cumulative-ack, in-flight-exit and realised-P&L alignment the `arm` process uses.

    The two wirings share one clock and one event order; the only thing that differs is
    what happens between the decision and the book. That is the variable under test.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    # Local imports: parity must stay importable from `listener` (which owns the trace hook)
    # without a module-level cycle through the listener itself.
    from .listener import Listener, TapeTransport

    origin = float(tape[0].ts) if tape else 0.0
    base = DEFAULT_BASE_TS if base_ts is None else float(base_ts)
    clock = TapeClock(base)
    scratch = Path(tempfile.mkdtemp(prefix=f"mff-parity-{mode}-"))
    engine = Engine(config=engine_config or EngineConfig(), fills=FillLog(), clock=clock)
    broker: Any = (PaperBroker() if mode == "replay"
                   else ShadowBroker(policy=venue or VenuePolicy()))
    script = {key: [(float(pair[0]), float(pair[1])) for pair in series]
              for key, series in quotes.items()}
    trace = Trace(mode=mode, tape=tape_name, pinned={
        "engine": {
            "pipe_test": engine.config.pipe_test,
            "enforce_expiry": engine.config.enforce_expiry,
            "enforce_breakeven": engine.config.enforce_breakeven,
            "max_spread_ratio": engine.config.max_spread_ratio,
        },
        "venue": (venue or VenuePolicy()).to_dict() if mode == "shadow" else {},
        "base_ts": base,
        "events": len(tape),
    })
    listener = Listener(
        transport=TapeTransport(list(tape)),
        engine=engine,
        broker=broker,
        quotes=QuoteScript(script),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=scratch / "heartbeat.json", clock=clock),
        kill=KillSwitch(path=scratch / "NO_TRADE", clock=clock),
        clock=clock,
        execution=ExecutionBook() if mode == "shadow" else None,
        trace=trace,
    )
    for message in tape:
        # The tape's relative spacing is preserved; only the origin is a real session, so
        # the calendar (ET day, expiry cutoff) sees a trading day rather than 1970.
        clock.set(base + (float(message.ts) - origin))
        listener.handle(message)
    return RunResult(trace=trace, listener=listener, broker=broker)


def compare_modes(tape: Sequence[RawMessage],
                  quotes: Mapping[str, Sequence[Sequence[float]]],
                  *, reference: str = "replay", candidate: str = "shadow",
                  engine_config: EngineConfig | None = None,
                  venue: VenuePolicy | None = None,
                  base_ts: float | None = None,
                  tape_name: str = "") -> ParityReport:
    """Run both wirings over one tape and compare them. The CLI's whole body."""
    for mode in (reference, candidate):
        if mode not in MODES:
            raise ValueError(f"unknown wiring {mode!r}; expected one of {MODES}")
    first = run_tape(tape, quotes, mode=reference, engine_config=engine_config,
                     venue=venue, base_ts=base_ts, tape_name=tape_name)
    second = run_tape(tape, quotes, mode=candidate, engine_config=engine_config,
                      venue=venue, base_ts=base_ts, tape_name=tape_name)
    report = compare(first.trace, second.trace)
    report.candidate.pinned["venue_broker"] = (
        second.broker.to_dict() if hasattr(second.broker, "to_dict") else {})
    return report


# ── the CLI's report helpers ─────────────────────────────────────────────────
def trace_from_fills(mode: str = "fills") -> Trace:
    """An empty trace with a mode label, for tests and tooling."""
    return Trace(mode=mode)

"""Explicitly armed, supervised external equity PAPER worker.

No authority to initialize an account, rearm a latch, change policy, train, or
promote. The source journal and delivery ledger remain the execution truth.
"""
from __future__ import annotations

import asyncio
import hashlib
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from .config import MARKET_TZ
from .delivery import PaperDeliveryError, PaperDeliveryLedger, PaperDeliveryReport, paper_money
from .paper_venue import (
    AlpacaEquityPaperVenue,
    cancel_paper_order,
    dispatch_paper_intent,
    load_paper_risk_policy,
    recover_paper_orders,
    request_from_live_journal,
    sweep_overdue_paper_orders,
)
from .provenance import stable_hash
from .session_service import Journal, JournalError, decode_paper_record

# Fixed conservative operational settings; not optimizer-controlled.
_QUEUE_SIZE = 64
_HEARTBEAT_MAX_SECONDS = 3.0
_IDLE_RECONCILE_SECONDS = 15.0
_WORKING_RECONCILE_SECONDS = 8.0
# These denials occur before a dispatch is committed. Unknown errors fault.
_REFUSALS = frozenset({
    "durable intent too old for dispatch", "entry quote stale at dispatch",
    "paper route window expired", "paper dispatch validity expired",
    "paper order outside authorized regular-session window",
    "paper exposure reservation refused", "paper working-order cap",
    "chase requires terminal confirmed zero initial fill",
    "exit requires exact terminal call inventory",
    "paper sell inventory already reserved", "paper symbol not approved",
    "paper reconciliation blocks dispatch", "venue market closed",
    "paper safety or account gate closed", "paper safety gate closed",
    "paper supervisor admission closed",
})


@dataclass(frozen=True, slots=True)
class WorkerAuthorization:
    digest: str
    risk_pin: str
    opening_head: str
    exit_limits: tuple[tuple[str, Decimal], ...]


def load_worker_authorization(path: Path, pin: str) -> WorkerAuthorization:
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != pin:
        raise PaperDeliveryError("worker authorization pin mismatch")
    data = decode_paper_record(raw)
    if (
        data.keys() != {"schema", "authority", "risk_sha256", "opening_ledger_head",
                        "exit_limits"}
        or data["schema"] != "scorpion.supervised-paper-worker.v1"
        or data["authority"] != "PAPER_ONLY"
    ):
        raise PaperDeliveryError("worker authorization schema")
    risk_pin, head, limits = (
        data["risk_sha256"], data["opening_ledger_head"], data["exit_limits"],
    )
    if (
        not isinstance(risk_pin, str) or not re.fullmatch(r"[0-9a-f]{64}", risk_pin)
        or not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{64}", head)
        or not isinstance(limits, dict) or not 1 <= len(limits) <= 100
    ):
        raise PaperDeliveryError("worker authorization scope")
    prices = []
    for symbol, value in sorted(limits.items()):
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,11}", symbol):
            raise PaperDeliveryError("worker exit symbol")
        price = paper_money(value, positive=True)
        tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            if price % tick:
                raise PaperDeliveryError("worker exit tick")
        prices.append((symbol, price))
    return WorkerAuthorization(pin, risk_pin, head, tuple(prices))


@dataclass(frozen=True, slots=True)
class Candidate:
    seq: int
    call: str
    role: str
    symbol: str | None
    created: datetime
    source_sha256: str


class SupervisedPaperWorker:
    """A single account writer, running outside the asyncio hot path.

    The operator must separately initialize an exclusive, flat account and pin
    its opening head. Restart/resume is not authorized by this implementation.
    """

    def __init__(
        self, *, authorization: WorkerAuthorization, risk_path: Path, ledger_path: Path,
        source: Journal, audit_path: Path, latch_path: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        trip: Callable[[str], object] | None = None,
    ) -> None:
        policy = load_paper_risk_policy(risk_path, authorization.risk_pin)
        self._clock = clock
        self._trip = trip
        if clock().astimezone(MARKET_TZ).date() != policy.session_date:
            raise PaperDeliveryError("worker policy is not for the current session")
        self._ledger = PaperDeliveryLedger(ledger_path, policy)
        initial = self._ledger.status(expected_head=authorization.opening_head)
        if initial.events != 1 or initial.orders:
            raise PaperDeliveryError("worker requires opening ledger; resume not authorized")
        if set(dict(authorization.exit_limits)) != set(dict(policy.symbols_and_groups)):
            raise PaperDeliveryError("operator exit limits must cover exact paper roster")
        self._venue = AlpacaEquityPaperVenue(policy.account_id, clock=clock)
        self._source = source
        self._audit_path = audit_path
        self._latch_path = latch_path
        self._authorization = authorization
        self._head = initial.chain_sha256
        self._queue: queue.Queue[Candidate] = queue.Queue(maxsize=_QUEUE_SIZE)
        self._stop = threading.Event()
        self._admission_fault = threading.Event()
        self._last_health: float | None = None
        self._next_reconcile = 0.0
        self._blockers: tuple[str, ...] = ("initial_reconciliation_required",)
        self._audit: Journal | None = None

    def heartbeat(self) -> None:
        self._last_health = time.monotonic()

    def stop_admission(self) -> None:
        self._stop.set()

    def _gate(self) -> bool:
        self._source.check()
        if self._audit is not None:
            self._audit.check()
        heartbeat = self._last_health
        return (
            not self._stop.is_set() and not self._admission_fault.is_set()
            and heartbeat is not None
            and 0 <= time.monotonic() - heartbeat <= _HEARTBEAT_MAX_SECONDS
        )

    def offer(self, seq: int, record: dict[str, object]) -> None:
        """Called on the producer loop after admission, never after a network call."""
        kind = record.get("kind")
        if kind not in ("intent", "conditional_exit_intent"):
            return
        call = record.get("call_id")
        symbol = record.get("ticker")
        stamp = record.get("ts_utc")
        if not isinstance(call, str) or not isinstance(stamp, str):
            raise JournalError("worker candidate identity")
        created = datetime.fromisoformat(stamp)
        if created.tzinfo is None or created.utcoffset() is None:
            raise JournalError("worker candidate clock")
        role = "EXIT" if kind == "conditional_exit_intent" else (
            "CHASE" if record.get("chase") is True else "INITIAL"
        )
        if self._stop.is_set():
            raise JournalError("worker stopped before candidate admission")
        candidate = Candidate(
            seq, call, role, symbol if isinstance(symbol, str) else None, created,
            stable_hash({**record, "seq": seq}),
        )
        try:
            self._queue.put_nowait(candidate)
        except queue.Full as exc:
            self.stop_admission()
            raise JournalError("worker candidate queue saturated") from exc

    def _record(self, kind: str, **fields: object) -> None:
        if self._audit is None:
            raise JournalError("worker audit missing")
        self._audit.submit({
            "kind": kind, "ts_utc": self._clock().isoformat(), "authority": "RESEARCH_ONLY",
            "ledger_head": self._head, **fields,
        })

    def _result(self, report: PaperDeliveryReport, *, kind: str) -> None:
        self._head = report.chain_sha256
        self._blockers = report.blockers
        if set(report.blockers) & {
            "unexpected_external_order", "external_position_mismatch",
            "external_cash_residual_exceeds_buffer", "venue_trading_blocked",
        } and not self._admission_fault.is_set():
            self._admission_fault.set()
            if self._trip is not None:
                self._trip("worker_account_reconciliation_fault")
        self._record(
            kind, blockers=report.blockers, orders=len(report.orders),
            states=tuple((o.client_id, o.state) for o in report.orders),
            positions=report.expected_positions, overdue=report.overdue_working_ids,
            commitment=str(report.session_buy_commitment_usd),
            cash_residual=str(report.cash_residual_usd),
        )

    def _maintain(self) -> None:
        now = self._clock()
        current = self._ledger.status(expected_head=self._head)
        working = tuple(o for o in current.orders if not (o.observation and o.observation.terminal))
        overdue = any(now >= o.request.deadline for o in working)
        if overdue:
            result = sweep_overdue_paper_orders(
                self._ledger, self._venue, expected_head=self._head, clock=self._clock,
            )
        elif any(o.state in ("DISPATCH_UNCERTAIN", "CANCEL_UNCERTAIN", "UNKNOWN")
                 for o in working):
            result = recover_paper_orders(
                self._ledger, self._venue, expected_head=self._head, clock=self._clock,
            )
        else:
            snapshot = self._venue.snapshot(self._ledger.policy.session_date)
            result = self._ledger.reconcile(
                snapshot, now=self._clock(), expected_head=self._head,
            )
        self._result(result, kind="worker_reconciliation")
        working_now = any(not (o.observation and o.observation.terminal) for o in result.orders)
        # Revisit a deadline as soon as possible, but never spin on an ambiguous cancel.
        interval = _WORKING_RECONCILE_SECONDS if working_now else _IDLE_RECONCILE_SECONDS
        deadlines = [
            (o.request.deadline - self._clock()).total_seconds()
            for o in result.orders if o.observation and not o.observation.terminal
            and not o.cancel_requested
        ]
        future = [d for d in deadlines if d > 0]
        if future:
            interval = min(interval, max(0.1, min(future)))
        self._next_reconcile = time.monotonic() + interval

    def _disposition(self, c: Candidate, state: str, reason: str) -> None:
        self._record("worker_disposition", intent_seq=c.seq, call_id=c.call, role=c.role,
                     disposition=state, reason=reason, source_record_sha256=c.source_sha256)

    def _process(self, c: Candidate) -> None:
        # The asynchronous journal submission is not a durable admission.
        ttl = timedelta(seconds=10 if c.role == "EXIT" else 3)
        while self._source.durable_sequence < c.seq:
            if self._stop.is_set() or self._clock() - c.created > ttl:
                self._disposition(c, "NOT_DISPATCHED", "durable_ack_deadline")
                return
            self._source.check()
            self._stop.wait(0.01)
        if not self._gate() or not timedelta(0) <= self._clock() - c.created <= ttl:
            self._disposition(c, "NOT_DISPATCHED", "supervision_or_candidate_deadline")
            return

        current = self._ledger.status(expected_head=self._head)
        related = tuple(o for o in current.orders if o.request.call_id == c.call)
        if any(o.request.role == c.role for o in related):
            self._disposition(c, "NOT_DISPATCHED", "existing_logical_order")
            return
        exit_limit = None
        symbol: str | None
        if c.role == "EXIT":
            if (
                not related or any(o.observation is None or not o.observation.terminal
                                   for o in related)
                or sum(o.filled for o in related) == 0
            ):
                self._disposition(c, "NOT_DISPATCHED", "no_exact_terminal_inventory")
                return
            symbol = related[0].request.symbol
            exit_limit = dict(self._authorization.exit_limits)[symbol]
        else:
            symbol = c.symbol
            if symbol is None or symbol not in dict(self._authorization.exit_limits):
                self._disposition(c, "NOT_DISPATCHED", "symbol_outside_operator_roster")
                return
            if c.role == "CHASE" and (
                len(related) != 1 or related[0].observation is None
                or related[0].observation.state not in ("CANCELLED", "EXPIRED")
                or related[0].filled != 0
            ):
                self._disposition(c, "NOT_DISPATCHED", "initial_not_terminal_zero")
                return

        if any(
            not (o.observation and o.observation.terminal)
            and o.request.deadline <= self._clock() + timedelta(seconds=3)
            for o in current.orders
        ):
            self._disposition(c, "NOT_DISPATCHED", "working_order_deadline_priority")
            self._next_reconcile = 0.0
            return
        blocked = set(self._blockers)
        if c.role == "EXIT":
            blocked.discard("terminal_fill_outside_route_deadline")
        if blocked:
            self._disposition(c, "NOT_DISPATCHED", "reconciliation_not_clear")
            return

        try:
            request = request_from_live_journal(
                self._source.path, c.seq, policy=self._ledger.policy,
                now=self._clock(), ledger_report=current, exit_limit=exit_limit,
            )
            if (request.call_id, request.role, request.symbol) != (c.call, c.role, symbol):
                raise PaperDeliveryError("candidate differs from durable journal")
            result = dispatch_paper_intent(
                self._ledger, self._venue, journal=self._source.path, seq=c.seq,
                expected_head=self._head, latch_path=self._latch_path, exit_limit=exit_limit,
                supervisor_guard=self._gate, clock=self._clock,
            )
        except PaperDeliveryError as exc:
            if str(exc) not in _REFUSALS:
                raise
            self._disposition(c, "NOT_DISPATCHED", str(exc))
            # Keep the externally pinned CAS head; never adopt an unknown writer.
            self._ledger.status(expected_head=self._head)
            self._next_reconcile = 0.0
            return

        self._head = result.chain_sha256
        matching = [o for o in result.orders if o.client_id == request.client_id(self._ledger.policy)]
        if len(matching) != 1:
            raise PaperDeliveryError("dispatch result missing durable order identity")
        self._disposition(c, "DURABLE_DISPATCH", matching[0].state)
        # Even an acknowledged POST is not independent reconciliation.
        self._blockers = ("fresh_account_reconciliation_required",)
        self._next_reconcile = 0.0

    def _shutdown(self) -> None:
        # No new orders, including no speculative market liquidation. Only exact
        # observed working identities may receive one durably claimed cancellation.
        result = recover_paper_orders(
            self._ledger, self._venue, expected_head=self._head, clock=self._clock,
        )
        self._result(result, kind="worker_shutdown_reconcile")
        for order in result.orders:
            if order.observation and not order.observation.terminal and not order.cancel_requested:
                result = cancel_paper_order(
                    self._ledger, self._venue, client_id=order.client_id,
                    expected_head=self._head, clock=self._clock,
                )
                self._result(result, kind="worker_shutdown_cancel")
        result = recover_paper_orders(
            self._ledger, self._venue, expected_head=self._head, clock=self._clock,
        )
        self._result(result, kind="worker_shutdown_final")
        if result.blockers or result.expected_positions or any(
            not (o.observation and o.observation.terminal) for o in result.orders
        ):
            raise PaperDeliveryError("worker shutdown not independently flat and reconciled")

    def _loop(self) -> None:
        self._audit = Journal(self._audit_path, require_lossless=True)
        try:
            self._record("worker_start", authorization_sha256=self._authorization.digest,
                         policy_sha256=self._ledger.policy.digest,
                         training_authorized=False, live_authorized=False)
            while not self._stop.is_set():
                self._source.check()
                self._audit.check()
                if time.monotonic() >= self._next_reconcile:
                    self._maintain()
                try:
                    candidate = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                self._process(candidate)
        except Exception as exc:
            self.stop_admission()
            if self._trip is not None:
                self._trip(f"worker_fault:{type(exc).__name__}")
            self._record("worker_fault", reason=type(exc).__name__)
            raise
        finally:
            self.stop_admission()
            try:
                while True:
                    try:
                        candidate = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    self._disposition(candidate, "NOT_DISPATCHED", "supervisor_stopped")
                self._shutdown()
            finally:
                self._audit.close()

    async def run(self) -> None:
        # Cancelling to_thread alone does not stop a running thread. Close the
        # admission gate first and join the in-flight call before session sealing.
        task = asyncio.create_task(asyncio.to_thread(self._loop))
        try:
            await asyncio.shield(task)
        finally:
            self.stop_admission()
            await task

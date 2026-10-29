"""The durable outbox dispatcher: from an approved proposal to a recorded execution.

Between "authorized" and "sent" there are four ways to lose money, and each has its own
mechanism here:

1. **Double submission** - a retry after a timeout that actually filled. The envelope
   carries a generation-specific idempotency key, the executions table is UNIQUE on that
   key, and the sender receives the key so a real venue can dedupe too.
2. **Poison effects** - one effect that always fails, blocking the queue. Attempts are
   counted at claim time and the effect is dead-lettered after `max_attempts`.
3. **Two workers, one effect** - each claim leases the row inside a `BEGIN IMMEDIATE`
   transaction, so a second worker cannot see it until the lease expires.
4. **Retrying a decision** - a refusal (stale quote, dislocation, halted book) is
   terminal. The market said no; asking again is not a delivery problem.

The dispatcher never settles the book itself. It returns receipts; the caller applies
`Pipeline.settle`, because a fill that moved the book without the risk ledger (or the
reverse) is worse than one that moved neither.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from .authorization import OrderEnvelope, authorize
from .config import Policy
from .domain import BookState, Effect, EffectKind
from .store import Store

ORDER_KINDS = frozenset(
    {
        EffectKind.PROPOSE_OPEN,
        EffectKind.PROPOSE_ADD,
        EffectKind.PROPOSE_TRIM,
        EffectKind.PROPOSE_CLOSE,
    }
)


@dataclass(frozen=True, slots=True)
class Receipt:
    """What a sender reports back. `retryable` is the sender's judgement, not ours."""

    ok: bool
    broker_ref: str = ""
    fill_price: Decimal | None = None
    fill_qty: int = 0
    error: str = ""
    retryable: bool = False


class Sender(Protocol):
    def send(self, envelope: OrderEnvelope, *, now: datetime) -> Receipt: ...


@dataclass(frozen=True, slots=True)
class DispatchConfig:
    max_attempts: int = 5
    lease_seconds: int = 30
    base_backoff_seconds: int = 2
    max_backoff_seconds: int = 300
    batch: int = 10

    def backoff(self, attempts: int) -> int:
        """Exponential with a ceiling: 2, 4, 8, 16 ... capped, never zero."""
        if attempts < 1:
            return int(self.base_backoff_seconds)
        growth = int(self.base_backoff_seconds) * (2 ** (int(attempts) - 1))
        return int(min(int(self.max_backoff_seconds), growth))


@dataclass
class Breaker:
    """Stops hammering a venue that is failing.

    A dead venue plus a queue is how a hundred effects become a hundred dead letters in
    thirty seconds. After `failure_threshold` consecutive transport failures the breaker
    opens for `cooldown_seconds`, then allows one probe; a success closes it, a failure
    re-opens it for another cooldown.
    """

    failure_threshold: int = 3
    cooldown_seconds: int = 30
    failures: int = 0
    opened_until: datetime | None = None

    def allow(self, now: datetime) -> bool:
        if self.opened_until is None:
            return True
        if now >= self.opened_until:
            self.opened_until = None       # half-open: one probe may pass
            return True
        return False

    @property
    def is_open(self) -> bool:
        return self.opened_until is not None

    def record_success(self) -> None:
        self.failures = 0
        self.opened_until = None

    def record_failure(self, now: datetime) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_until = now + timedelta(seconds=self.cooldown_seconds)


@dataclass(frozen=True, slots=True)
class Dispatched:
    effect_id: int
    contract_key: str | None
    outcome: str          # SENT | REFUSED | RETRY | DEAD
    detail: str
    envelope: OrderEnvelope | None = None
    receipt: Receipt | None = None


@dataclass
class DispatchReport:
    attempted: int = 0
    sent: int = 0
    refused: int = 0
    retried: int = 0
    dead: int = 0
    items: list[Dispatched] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "sent": self.sent,
            "refused": self.refused,
            "retried": self.retried,
            "dead": self.dead,
            "items": [
                {"effect_id": i.effect_id, "contract_key": i.contract_key,
                 "outcome": i.outcome, "detail": i.detail}
                for i in self.items
            ],
        }


@dataclass(slots=True)
class Dispatcher:
    store: Store
    sender: Sender
    policy: Policy = field(default_factory=Policy)
    config: DispatchConfig = field(default_factory=DispatchConfig)
    owner: str = "dispatcher"
    # Called the instant a send succeeds. The book must move before the NEXT effect in the
    # same batch is authorized, or a close can be authorized against a position the open in
    # that batch has not filled yet.
    on_filled: Callable[[Dispatched], None] | None = None
    breaker: Breaker = field(default_factory=Breaker)
    recover_after_seconds: int = 0

    def dispatch_once(
        self,
        *,
        state: BookState,
        now: datetime | None = None,
        quotes: Mapping[str, object] | None = None,
        references: Mapping[str, Decimal] | None = None,
    ) -> DispatchReport:
        """One bounded pass over what is due. Safe to call in a loop; never blocks."""
        moment = now or datetime.now(UTC)

        # A row still SENDING is an ambiguous send from a previous process: quarantine it
        # before claiming anything, because retrying it could double a real order.
        self.store.recover_sending(now=moment, older_than_seconds=self.recover_after_seconds)

        if not self.breaker.allow(moment):
            report = DispatchReport()
            report.items.append(
                Dispatched(0, None, "CIRCUIT_OPEN", f"until {self.breaker.opened_until}")
            )
            self.store.heartbeat("outbox_dispatcher", circuit="open")
            return report

        claims = self.store.claim_due(
            owner=self.owner, now=moment, limit=self.config.batch,
            lease_seconds=self.config.lease_seconds,
        )
        report = DispatchReport(attempted=len(claims))
        for claim in claims:
            item = self._dispatch_one(claim, state=state, now=moment, quotes=quotes,
                                      references=references)
            report.items.append(item)
            if item.outcome == "SENT" and self.on_filled is not None:
                self.on_filled(item)
        for item in report.items:
            if item.outcome == "SENT":
                report.sent += 1
            elif item.outcome == "REFUSED":
                report.refused += 1
            elif item.outcome == "DEAD":
                report.dead += 1
            else:
                report.retried += 1
        self.store.heartbeat(
            "outbox_dispatcher",
            attempted=report.attempted, sent=report.sent, refused=report.refused,
            retried=report.retried, dead=report.dead,
        )
        return report

    def _dispatch_one(
        self,
        claim: dict[str, object],
        *,
        state: BookState,
        now: datetime,
        quotes: Mapping[str, object] | None,
        references: Mapping[str, Decimal] | None,
    ) -> Dispatched:
        effect_id = int(str(claim["effect_id"]))
        kind = EffectKind(str(claim["kind"]))
        contract_key = str(claim["contract_key"]) if claim["contract_key"] else None
        tries = int(str(claim["attempts"]))

        effect = Effect(
            kind=kind,
            contract_key=contract_key,
            source_event_id=str(claim["source_event_id"]),
            generation=int(str(claim["generation"])),
            reason=str(claim["reason"]),
            quantity_hint=(
                int(str(claim["quantity_hint"])) if claim["quantity_hint"] is not None else None
            ),
        )

        # HALT / REVIEW effects are not orders. They exit the queue as a refusal so the
        # outbox stays a queue of *deliverables*, and the review trail lives in the log.
        if kind not in ORDER_KINDS:
            self.store.mark_refused(
                effect_id=effect_id, reason=f"not_an_order_effect:{kind.value}", now=now
            )
            return Dispatched(effect_id, contract_key, "REFUSED", "not_an_order_effect")

        quote = None if quotes is None else quotes.get(contract_key or "")
        reference = None if references is None else references.get(contract_key or "")
        auth = authorize(
            effect,
            state=state,
            quote=quote,  # type: ignore[arg-type]
            policy=self.policy,
            now_utc=now,
            reference_price=reference,
        )
        if not auth.ok or auth.envelope is None:
            self.store.mark_refused(effect_id=effect_id, reason=auth.reason, now=now)
            return Dispatched(effect_id, contract_key, "REFUSED", auth.reason)

        try:
            receipt = self.sender.send(auth.envelope, now=now)
        except Exception as exc:  # noqa: BLE001 - a sender failure is data, not a crash
            # An exception means we do NOT know whether the venue received the order: a
            # timeout can hide a fill. Quarantine it as UNKNOWN and let reconciliation
            # decide, because a blind retry here is how one order becomes two.
            self.store.mark_unknown(
                effect_id=effect_id, reason=f"{type(exc).__name__}: {exc}", now=now,
                idempotency_key=auth.envelope.idempotency_key,
            )
            self.breaker.record_failure(now)
            return Dispatched(effect_id, contract_key, "UNKNOWN", f"sender_error:{exc}",
                              envelope=auth.envelope)

        if receipt.ok:
            self.store.record_execution(
                effect_id=effect_id,
                idempotency_key=auth.envelope.idempotency_key,
                contract_key=auth.envelope.contract_key,
                side=auth.envelope.side,
                quantity=auth.envelope.quantity,
                limit_price=str(auth.envelope.limit_price),
                fill_price=str(receipt.fill_price if receipt.fill_price is not None
                               else auth.envelope.limit_price),
                fill_qty=receipt.fill_qty or auth.envelope.quantity,
                intent_ts=now,
                fill_ts=now,
                broker_ref=receipt.broker_ref,
            )
            self.store.mark_sent(effect_id=effect_id, broker_ref=receipt.broker_ref, now=now)
            self.breaker.record_success()
            return Dispatched(effect_id, contract_key, "SENT", receipt.broker_ref,
                              envelope=auth.envelope, receipt=receipt)

        if receipt.retryable:
            # An explicit "not filled" from the venue is safe to retry; only the ambiguity
            # of a transport exception is not.
            self.breaker.record_failure(now)
            status = self.store.mark_failed(
                effect_id=effect_id, error=receipt.error, now=now,
                backoff_seconds=self.config.backoff(tries),
                max_attempts=self.config.max_attempts,
            )
            return Dispatched(effect_id, contract_key, status, receipt.error,
                              envelope=auth.envelope, receipt=receipt)

        self.store.mark_refused(effect_id=effect_id, reason=receipt.error, now=now)
        return Dispatched(effect_id, contract_key, "REFUSED", receipt.error,
                          envelope=auth.envelope, receipt=receipt)

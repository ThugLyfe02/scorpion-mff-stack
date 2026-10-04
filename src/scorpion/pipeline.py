from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .association import associate_followup_with_evidence
from .decision_packet import DecisionDisposition, OperatorDecisionPacket, build_decision_packet
from .domain import BookState, Effect, RawDiscordMessage, SignalEvent
from .eligibility import classify_eligibility
from .execution_state import replay_admitted_events
from .failure_quarantine import (
    FailureDisposition,
    QuarantinedRawRevision,
    load_quarantined,
    record_processing_failure,
)
from .integrity import ReplayIntegritySentinel, verify_database_evidence
from .invariants import assert_valid_book
from .parser import parse_message_with_evidence
from .policy_bundle import RuntimePolicyBundle
from .processing_order import (
    ensure_processing_order_schema,
    load_pending_raw_in_receipt_order,
    load_signals_in_processing_order,
    register_raw_receipt,
)
from .reducer import reduce_book
from .replay import ReplayOrder, state_fingerprint
from .resilience import OperationalMode, ResilienceAssessment
from .sequence_guard import assess_sequence
from .source_intelligence import SourceBehaviorShift
from .state_checkpoint import restore_state
from .store import Store
from .transactional import RuntimeHaltedError as RuntimeHaltedError
from .transactional import SQLiteTransitionCommitter, TransitionCommitter

SourceShiftResolver = Callable[[str, str], SourceBehaviorShift | None]


def _elapsed_us(started_ns: int) -> int:
    return max(0, (time.perf_counter_ns() - started_ns) // 1_000)


def _normal_resilience() -> ResilienceAssessment:
    return ResilienceAssessment(OperationalMode.NORMAL, (), ())


@dataclass(slots=True)
class Pipeline:
    store: Store
    allowed_author_ids: frozenset[str] | None = None
    committer: TransitionCommitter = field(default_factory=SQLiteTransitionCommitter)
    resilience_assessment: ResilienceAssessment = field(default_factory=_normal_resilience)
    source_shift_resolver: SourceShiftResolver | None = None
    runtime_policy: RuntimePolicyBundle = field(default_factory=RuntimePolicyBundle)
    maximum_raw_attempts: int = 3
    integrity_sentinel: ReplayIntegritySentinel = field(default_factory=ReplayIntegritySentinel)
    state: BookState = field(init=False)
    observed_state: BookState = field(init=False)
    recent_events: list[SignalEvent] = field(init=False)
    _transition_lock: threading.Lock = field(init=False, repr=False)

    def _rebuild_states(self) -> None:
        historical_signals = load_signals_in_processing_order(self.store.path)
        restored = restore_state(
            self.store.path,
            historical_signals,
            runtime_policy=self.runtime_policy,
        )
        # Existing checkpoints contain all normalized observations. They do not
        # attest which events were admitted to execution state.
        self.observed_state = restored.state
        self.state, _ = replay_admitted_events(
            self.store.path,
            historical_signals,
            policy=self.runtime_policy.base,
            order=ReplayOrder.INPUT,
        )
        assert_valid_book(
            self.observed_state,
            max_open_positions=self.runtime_policy.base.max_open_positions,
        )
        assert_valid_book(
            self.state,
            max_open_positions=self.runtime_policy.base.max_open_positions,
        )
        # Atomic normalized commits already persist their effects. Recreating
        # them here could disguise missing evidence or promote an effect computed
        # under a different replay policy. An inconsistent store stays halted.
        evidence = verify_database_evidence(self.store.path)
        if not evidence.ok or evidence.legacy_uncovered_signals:
            self.store.set_halt(True, "startup_evidence_integrity_failed")
        self.recent_events = historical_signals[-100:]
        self.store.heartbeat(
            "replay-recovery",
            used_checkpoint=restored.used_checkpoint,
            checkpoint_id=restored.checkpoint_id,
            tail_events=restored.tail_events,
            reason=restored.reason,
            state_fingerprint=state_fingerprint(self.state),
            observed_state_fingerprint=state_fingerprint(self.observed_state),
            checkpoint_state_scope="OBSERVED",
            policy_fingerprint=self.runtime_policy.fingerprint,
            replay_order="durable_process_seq",
        )

    def __post_init__(self) -> None:
        if self.maximum_raw_attempts <= 0:
            raise ValueError("maximum_raw_attempts must be positive")
        self._transition_lock = threading.Lock()
        with self.store.connect() as db:
            ensure_processing_order_schema(db)
        self._rebuild_states()

        halted, _ = self.store.runtime_halt()
        if halted:
            return
        quarantined = load_quarantined(self.store.path, limit=1)
        if quarantined:
            self.store.set_halt(True, f"raw_quarantined:{quarantined[0].raw_event_id}")
            return

        for raw in load_pending_raw_in_receipt_order(self.store.path):
            try:
                self._process(raw, persist_raw=False)
            except QuarantinedRawRevision as exc:
                self.store.set_halt(True, f"raw_quarantined:{exc.raw_event_id}")
                self.store.heartbeat(
                    "replay-recovery",
                    status="quarantined_raw",
                    raw_revision_id=exc.raw_event_id,
                    attempt_count=exc.attempt_count,
                    policy_fingerprint=self.runtime_policy.fingerprint,
                )
                break
            except RuntimeHaltedError:
                # A halt can be latched between recovery admission and its commit.
                # Leave this and all later revisions pending for operator recovery.
                break
            except Exception as exc:
                self.store.set_halt(True, f"raw_recovery_failed:{raw.revision_id}")
                raise RuntimeError("failed to recover pending raw Discord revision") from exc

    def _source_shift(self, raw: RawDiscordMessage) -> SourceBehaviorShift | None:
        if self.source_shift_resolver is None:
            return None
        return self.source_shift_resolver(raw.author_id, raw.channel_id)

    def _association_state(self) -> BookState:
        """Merge research-observed and admitted positions for source-lineage association only."""
        positions = dict(self.observed_state.positions)
        positions.update(self.state.positions)
        days = [
            day
            for day in (
                self.observed_state.first_entry_proposed_on,
                self.state.first_entry_proposed_on,
            )
            if day is not None
        ]
        return BookState(
            positions=positions,
            halted=self.observed_state.halted or self.state.halted,
            seen_event_ids=self.observed_state.seen_event_ids | self.state.seen_event_ids,
            first_entry_proposed_on=max(days) if days else None,
        )

    def _process(
        self,
        raw: RawDiscordMessage,
        *,
        persist_raw: bool,
    ) -> tuple[SignalEvent, tuple[Effect, ...]]:
        started_ns = time.perf_counter_ns()
        raw_persist_us = 0
        if persist_raw:
            raw_started_ns = time.perf_counter_ns()
            self.store.append_raw(raw)
            register_raw_receipt(self.store.path, raw.revision_id)
            raw_persist_us = _elapsed_us(raw_started_ns)
        halted, halt_reason = self.store.runtime_halt()
        if halted:
            self.store.heartbeat(
                "pipeline",
                last_message_id=raw.message_id,
                last_revision_id=raw.revision_id,
                status="halted",
                halt_reason=halt_reason,
            )
            raise RuntimeHaltedError(f"runtime halted: {halt_reason}")
        try:
            parse_started_ns = time.perf_counter_ns()
            parsed = parse_message_with_evidence(raw, self.allowed_author_ids)
            parse_us = _elapsed_us(parse_started_ns)

            association_started_ns = time.perf_counter_ns()
            referenced_key = (
                self.store.contract_for_message(raw.referenced_message_id)
                if raw.referenced_message_id
                else None
            )
            association_state = self._association_state()
            associated = associate_followup_with_evidence(
                parsed.event,
                association_state,
                referenced_key,
            )
            association_us = _elapsed_us(association_started_ns)

            reduce_started_ns = time.perf_counter_ns()
            event = associated.event
            eligibility = classify_eligibility(event, self.runtime_policy.eligibility)
            sequence = assess_sequence(
                event,
                association_state,
                recent_events=self.recent_events,
            )
            packet: OperatorDecisionPacket = build_decision_packet(
                event,
                parsed.evidence,
                associated.evidence,
                self.resilience_assessment,
                sequence,
                source_shift=self._source_shift(raw),
                eligibility=eligibility,
                policy=self.runtime_policy.selective_review,
                policy_fingerprint=self.runtime_policy.fingerprint,
            )
            observed_proposed_state, observed_effects = reduce_book(
                self.observed_state,
                event,
                self.runtime_policy.base,
            )
            assert_valid_book(
                observed_proposed_state,
                max_open_positions=self.runtime_policy.base.max_open_positions,
            )
            blocked_from_execution_state = packet.disposition in {
                DecisionDisposition.BLOCKED_STRATEGY,
                DecisionDisposition.BLOCKED_SYSTEM,
            }
            if blocked_from_execution_state:
                proposed_state = self.state
                proposed_effects = observed_effects
            else:
                proposed_state, proposed_effects = reduce_book(
                    self.state,
                    event,
                    self.runtime_policy.base,
                )
                assert_valid_book(
                    proposed_state,
                    max_open_positions=self.runtime_policy.base.max_open_positions,
                )
            proposed_fingerprint = state_fingerprint(proposed_state)
            reduce_validate_us = _elapsed_us(reduce_started_ns)

            stage_latencies_us = {
                "raw_persist_us": raw_persist_us,
                "parse_us": parse_us,
                "association_us": association_us,
                "reduce_validate_us": reduce_validate_us,
            }
            result = self.committer.commit(
                self.store,
                raw_revision_id=raw.revision_id,
                event=event,
                effects=proposed_effects,
                parser=parsed.evidence,
                association=associated.evidence,
                decision_packet=packet,
                started_ns=started_ns,
                heartbeat_metadata={
                    "last_message_id": raw.message_id,
                    "last_revision_id": raw.revision_id,
                    "last_event_id": event.event_id,
                    "effect_count": len(proposed_effects),
                    "parser_latency_us": parsed.evidence.latency_us,
                    "state_fingerprint": proposed_fingerprint,
                    "observed_state_fingerprint": state_fingerprint(observed_proposed_state),
                    "decision_disposition": packet.disposition.value,
                    "operational_mode": packet.system_mode.value,
                    "policy_fingerprint": self.runtime_policy.fingerprint,
                    "replay_order": "durable_process_seq",
                },
                stage_latencies_us=stage_latencies_us,
            )
        except (QuarantinedRawRevision, RuntimeHaltedError):
            # Admission denial is not a failed normalization attempt.
            raise
        except Exception as exc:
            failure = record_processing_failure(
                self.store.path,
                raw.revision_id,
                type(exc).__name__,
                maximum_attempts=self.maximum_raw_attempts,
            )
            self.store.heartbeat(
                "pipeline",
                last_message_id=raw.message_id,
                last_revision_id=raw.revision_id,
                status=(
                    "quarantined"
                    if failure.disposition is FailureDisposition.QUARANTINED
                    else "error"
                ),
                error=type(exc).__name__,
                raw_failure_attempts=failure.attempt_count,
                policy_fingerprint=self.runtime_policy.fingerprint,
            )
            if failure.disposition is FailureDisposition.QUARANTINED:
                raise QuarantinedRawRevision(
                    raw.revision_id,
                    failure.attempt_count,
                    failure.error,
                ) from exc
            raise

        # The normalized transaction, including raw DONE, has committed. Failures
        # below must never rewrite that completion marker as a retryable failure.
        try:
            if result.inserted:
                self.observed_state = observed_proposed_state
                if not blocked_from_execution_state:
                    self.state = proposed_state
                self.recent_events.append(event)
                self.recent_events = self.recent_events[-100:]
                effects = proposed_effects
                self.integrity_sentinel.after_commit(
                    self.store,
                    self.state,
                    observed_state=self.observed_state,
                    runtime_policy=self.runtime_policy,
                )
            else:
                effects = ()
                if event.event_id not in self.observed_state.seen_event_ids:
                    self._rebuild_states()
            return event, effects
        except Exception as exc:
            halted, _ = self.store.runtime_halt()
            if not halted:
                self.store.set_halt(True, f"post_commit_verification_failed:{type(exc).__name__}")
            raise

    def _process_serialized(
        self,
        raw: RawDiscordMessage,
        *,
        persist_raw: bool,
        register_receipt: bool,
    ) -> tuple[SignalEvent, tuple[Effect, ...]]:
        with self._transition_lock:
            if register_receipt:
                register_raw_receipt(self.store.path, raw.revision_id)
            return self._process(raw, persist_raw=persist_raw)

    async def handle(self, raw: RawDiscordMessage) -> tuple[SignalEvent, tuple[Effect, ...]]:
        return await asyncio.to_thread(
            self._process_serialized,
            raw,
            persist_raw=True,
            register_receipt=False,
        )

    async def handle_persisted(
        self,
        raw: RawDiscordMessage,
    ) -> tuple[SignalEvent, tuple[Effect, ...]]:
        """Process a durable raw revision on a worker thread under one transition lock."""
        return await asyncio.to_thread(
            self._process_serialized,
            raw,
            persist_raw=False,
            register_receipt=True,
        )

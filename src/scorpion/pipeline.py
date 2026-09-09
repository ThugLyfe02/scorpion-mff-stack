from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .association import associate_followup_with_evidence
from .decision_packet import OperatorDecisionPacket, build_decision_packet
from .domain import BookState, Effect, RawDiscordMessage, SignalEvent
from .eligibility import classify_eligibility
from .failure_quarantine import (
    FailureDisposition,
    QuarantinedRawRevision,
    record_processing_failure,
)
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
from .replay import state_fingerprint
from .resilience import OperationalMode, ResilienceAssessment
from .sequence_guard import assess_sequence
from .source_intelligence import SourceBehaviorShift
from .state_checkpoint import restore_state
from .store import Store
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
    state: BookState = field(init=False)
    recent_events: list[SignalEvent] = field(init=False)

    def __post_init__(self) -> None:
        if self.maximum_raw_attempts <= 0:
            raise ValueError("maximum_raw_attempts must be positive")
        with self.store.connect() as db:
            ensure_processing_order_schema(db)
        historical_signals = load_signals_in_processing_order(self.store.path)
        restored = restore_state(
            self.store.path,
            historical_signals,
            runtime_policy=self.runtime_policy,
        )
        self.state = restored.state
        assert_valid_book(
            self.state,
            max_open_positions=self.runtime_policy.base.max_open_positions,
        )
        self.store.append_effects(restored.effects_to_rematerialize)
        self.recent_events = historical_signals[-100:]
        self.store.heartbeat(
            "replay-recovery",
            used_checkpoint=restored.used_checkpoint,
            checkpoint_id=restored.checkpoint_id,
            tail_events=restored.tail_events,
            reason=restored.reason,
            state_fingerprint=state_fingerprint(self.state),
            policy_fingerprint=self.runtime_policy.fingerprint,
            replay_order="durable_process_seq",
        )

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
                continue
            except Exception as exc:
                self.store.set_halt(True, f"raw_recovery_failed:{raw.revision_id}")
                raise RuntimeError("failed to recover pending raw Discord revision") from exc

    def _source_shift(self, raw: RawDiscordMessage) -> SourceBehaviorShift | None:
        if self.source_shift_resolver is None:
            return None
        return self.source_shift_resolver(raw.author_id, raw.channel_id)

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
            associated = associate_followup_with_evidence(
                parsed.event,
                self.state,
                referenced_key,
            )
            association_us = _elapsed_us(association_started_ns)

            reduce_started_ns = time.perf_counter_ns()
            event = associated.event
            sequence = assess_sequence(event, self.state, recent_events=self.recent_events)
            eligibility = classify_eligibility(event, self.runtime_policy.eligibility)
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
                    "decision_disposition": packet.disposition.value,
                    "operational_mode": packet.system_mode.value,
                    "policy_fingerprint": self.runtime_policy.fingerprint,
                    "replay_order": "durable_process_seq",
                },
                stage_latencies_us=stage_latencies_us,
            )
            if result.inserted:
                self.state = proposed_state
                self.recent_events.append(event)
                self.recent_events = self.recent_events[-100:]
                effects = proposed_effects
            else:
                effects = ()
                if event.event_id not in self.state.seen_event_ids:
                    historical_signals = load_signals_in_processing_order(self.store.path)
                    restored = restore_state(
                        self.store.path,
                        historical_signals,
                        runtime_policy=self.runtime_policy,
                    )
                    self.state = restored.state
                    self.recent_events = historical_signals[-100:]
                    assert_valid_book(
                        self.state,
                        max_open_positions=self.runtime_policy.base.max_open_positions,
                    )
            return event, effects
        except QuarantinedRawRevision:
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

    async def handle(self, raw: RawDiscordMessage) -> tuple[SignalEvent, tuple[Effect, ...]]:
        return self._process(raw, persist_raw=True)

    async def handle_persisted(
        self,
        raw: RawDiscordMessage,
    ) -> tuple[SignalEvent, tuple[Effect, ...]]:
        """Process a raw revision that has already crossed the durable receipt boundary."""
        register_raw_receipt(self.store.path, raw.revision_id)
        return self._process(raw, persist_raw=False)

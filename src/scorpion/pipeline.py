from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .association import associate_followup_with_evidence
from .decision_packet import OperatorDecisionPacket, build_decision_packet
from .domain import BookState, Effect, RawDiscordMessage, SignalEvent
from .eligibility import classify_eligibility
from .invariants import assert_valid_book
from .parser import parse_message_with_evidence
from .policy_bundle import RuntimePolicyBundle
from .reducer import reduce_book
from .replay import replay, state_fingerprint
from .resilience import OperationalMode, ResilienceAssessment
from .sequence_guard import assess_sequence
from .source_intelligence import SourceBehaviorShift
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
    state: BookState = field(init=False)
    recent_events: list[SignalEvent] = field(init=False)

    def __post_init__(self) -> None:
        historical_signals = self.store.load_signals()
        self.state, historical_effects = replay(
            historical_signals,
            self.runtime_policy.base,
        )
        assert_valid_book(
            self.state,
            max_open_positions=self.runtime_policy.base.max_open_positions,
        )
        self.store.append_effects(historical_effects)
        self.recent_events = historical_signals[-100:]

        for raw in self.store.load_pending_raw():
            try:
                self._process(raw, persist_raw=False)
            except Exception as exc:
                self.store.mark_raw_failed(raw.revision_id, type(exc).__name__)
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
                    historical_signals = self.store.load_signals()
                    self.state, _ = replay(
                        historical_signals,
                        self.runtime_policy.base,
                    )
                    self.recent_events = historical_signals[-100:]
                    assert_valid_book(
                        self.state,
                        max_open_positions=self.runtime_policy.base.max_open_positions,
                    )
            return event, effects
        except Exception as exc:
            self.store.mark_raw_failed(raw.revision_id, type(exc).__name__)
            self.store.heartbeat(
                "pipeline",
                last_message_id=raw.message_id,
                last_revision_id=raw.revision_id,
                status="error",
                error=type(exc).__name__,
                policy_fingerprint=self.runtime_policy.fingerprint,
            )
            raise

    async def handle(self, raw: RawDiscordMessage) -> tuple[SignalEvent, tuple[Effect, ...]]:
        return self._process(raw, persist_raw=True)

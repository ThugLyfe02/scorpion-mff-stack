from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .association import associate_followup_with_evidence
from .decision_packet import DecisionDisposition, OperatorDecisionPacket, build_decision_packet
from .domain import BookState, Effect, RawDiscordMessage, SignalEvent
from .execution_state import replay_admitted_events
from .invariants import assert_valid_book
from .parser import parse_message_with_evidence
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
    state: BookState = field(init=False)
    observed_state: BookState = field(init=False)
    recent_events: list[SignalEvent] = field(init=False)

    def _rebuild_states(self) -> None:
        historical_signals = self.store.load_signals()
        self.observed_state, _ = replay(historical_signals)
        self.state, execution_effects = replay_admitted_events(
            self.store.path,
            historical_signals,
        )
        assert_valid_book(self.observed_state)
        assert_valid_book(self.state)
        self.store.append_effects(execution_effects)
        self.recent_events = historical_signals[-100:]

    def __post_init__(self) -> None:
        self._rebuild_states()

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
            association_state = self._association_state()
            associated = associate_followup_with_evidence(
                parsed.event,
                association_state,
                referenced_key,
            )
            association_us = _elapsed_us(association_started_ns)

            reduce_started_ns = time.perf_counter_ns()
            event = associated.event
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
            )
            observed_proposed_state, observed_effects = reduce_book(self.observed_state, event)
            assert_valid_book(observed_proposed_state)

            blocked_from_execution_state = packet.disposition in {
                DecisionDisposition.BLOCKED_STRATEGY,
                DecisionDisposition.BLOCKED_SYSTEM,
            }
            if blocked_from_execution_state:
                proposed_state = self.state
                proposed_effects = observed_effects
            else:
                proposed_state, proposed_effects = reduce_book(self.state, event)
                assert_valid_book(proposed_state)
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
                },
                stage_latencies_us=stage_latencies_us,
            )
            if result.inserted:
                self.observed_state = observed_proposed_state
                if not blocked_from_execution_state:
                    self.state = proposed_state
                self.recent_events.append(event)
                self.recent_events = self.recent_events[-100:]
                effects = proposed_effects
            else:
                effects = ()
                if event.event_id not in self.observed_state.seen_event_ids:
                    self._rebuild_states()
            return event, effects
        except Exception as exc:
            self.store.mark_raw_failed(raw.revision_id, type(exc).__name__)
            self.store.heartbeat(
                "pipeline",
                last_message_id=raw.message_id,
                last_revision_id=raw.revision_id,
                status="error",
                error=type(exc).__name__,
            )
            raise

    async def handle(self, raw: RawDiscordMessage) -> tuple[SignalEvent, tuple[Effect, ...]]:
        return self._process(raw, persist_raw=True)

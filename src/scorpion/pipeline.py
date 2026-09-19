from __future__ import annotations

import time
from dataclasses import dataclass, field

from .association import associate_followup_with_evidence
from .domain import BookState, Effect, RawDiscordMessage, SignalEvent
from .invariants import assert_valid_book
from .parser import parse_message_with_evidence
from .reducer import reduce_book
from .replay import replay, state_fingerprint
from .store import Store
from .transactional import SQLiteTransitionCommitter, TransitionCommitter


@dataclass(slots=True)
class Pipeline:
    store: Store
    allowed_author_ids: frozenset[str] | None = None
    committer: TransitionCommitter = field(default_factory=SQLiteTransitionCommitter)
    state: BookState = field(init=False)

    def __post_init__(self) -> None:
        self.state, historical_effects = replay(self.store.load_signals())
        assert_valid_book(self.state)
        self.store.append_effects(historical_effects)

        for raw in self.store.load_pending_raw():
            try:
                self._process(raw, persist_raw=False)
            except Exception as exc:
                self.store.mark_raw_failed(raw.revision_id, type(exc).__name__)
                self.store.set_halt(True, f"raw_recovery_failed:{raw.revision_id}")
                raise RuntimeError("failed to recover pending raw Discord revision") from exc

    def _process(
        self,
        raw: RawDiscordMessage,
        *,
        persist_raw: bool,
    ) -> tuple[SignalEvent, tuple[Effect, ...]]:
        started_ns = time.perf_counter_ns()
        if persist_raw:
            self.store.append_raw(raw)
        try:
            parsed = parse_message_with_evidence(raw, self.allowed_author_ids)
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
            event = associated.event
            proposed_state, proposed_effects = reduce_book(self.state, event)
            assert_valid_book(proposed_state)
            proposed_fingerprint = state_fingerprint(proposed_state)
            result = self.committer.commit(
                self.store,
                raw_revision_id=raw.revision_id,
                event=event,
                effects=proposed_effects,
                parser=parsed.evidence,
                association=associated.evidence,
                started_ns=started_ns,
                heartbeat_metadata={
                    "last_message_id": raw.message_id,
                    "last_revision_id": raw.revision_id,
                    "last_event_id": event.event_id,
                    "effect_count": len(proposed_effects),
                    "parser_latency_us": parsed.evidence.latency_us,
                    "state_fingerprint": proposed_fingerprint,
                },
            )
            if result.inserted:
                self.state = proposed_state
                effects = proposed_effects
            else:
                effects = ()
                if event.event_id not in self.state.seen_event_ids:
                    self.state, _ = replay(self.store.load_signals())
                    assert_valid_book(self.state)
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

from __future__ import annotations

from dataclasses import dataclass, field

from .association import associate_followup
from .domain import BookState, Effect, RawDiscordMessage, SignalEvent
from .parser import parse_message
from .replay import replay
from .reducer import reduce_book
from .store import Store


@dataclass(slots=True)
class Pipeline:
    store: Store
    allowed_author_ids: frozenset[str] | None = None
    state: BookState = field(init=False)

    def __post_init__(self) -> None:
        # Event-sourced crash recovery: rebuild deterministic book state from durable normalized events.
        self.state, historical_effects = replay(self.store.load_signals())
        # INSERT OR IGNORE re-materializes any effect lost by a crash after signal persistence.
        self.store.append_effects(historical_effects)

    async def handle(self, raw: RawDiscordMessage) -> tuple[SignalEvent, tuple[Effect, ...]]:
        self.store.append_raw(raw)
        event = parse_message(raw, self.allowed_author_ids)
        referenced_key = (
            self.store.contract_for_message(raw.referenced_message_id)
            if raw.referenced_message_id
            else None
        )
        event = associate_followup(event, self.state, referenced_key)
        inserted = self.store.append_signal(event)
        if not inserted:
            # Exact duplicate/reconnect delivery. State already includes this event.
            return event, ()
        self.state, effects = reduce_book(self.state, event)
        self.store.append_effects(effects)
        self.store.heartbeat(
            "pipeline",
            last_message_id=raw.message_id,
            last_event_id=event.event_id,
            effect_count=len(effects),
        )
        return event, effects

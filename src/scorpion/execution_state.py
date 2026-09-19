from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from .domain import BookState, Effect, SignalEvent
from .replay import replay

_BLOCKING_DISPOSITIONS = frozenset({"BLOCKED_STRATEGY", "BLOCKED_SYSTEM"})


def load_blocked_event_ids(path: str | Path) -> frozenset[str]:
    """Return events that were durably denied admission to execution state."""
    db = sqlite3.connect(str(path))
    try:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_decision_packets'"
        ).fetchone()
        if table is None:
            return frozenset()
        rows = db.execute(
            "SELECT event_id FROM operator_decision_packets "
            "WHERE disposition IN ('BLOCKED_STRATEGY','BLOCKED_SYSTEM')"
        ).fetchall()
    finally:
        db.close()
    return frozenset(str(row[0]) for row in rows)


def admitted_events(
    path: str | Path,
    events: Sequence[SignalEvent],
) -> tuple[SignalEvent, ...]:
    blocked = load_blocked_event_ids(path)
    return tuple(event for event in events if event.event_id not in blocked)


def replay_admitted_events(
    path: str | Path,
    events: Sequence[SignalEvent],
) -> tuple[BookState, tuple[Effect, ...]]:
    """Replay only events that were not durably blocked from execution admission.

    Legacy signals without an operator decision packet remain admitted for backwards compatibility.
    """
    return replay(admitted_events(path, events))

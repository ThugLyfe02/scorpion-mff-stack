from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .accuracy import score_decisions
from .domain import EventKind, SignalEvent
from .replay import replay, state_fingerprint
from .store import Store


def health_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scorpion.db")
    args = parser.parse_args()
    print(json.dumps(Store(args.db).health_snapshot(), indent=2, sort_keys=True))


def accuracy_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scorpion.db")
    args = parser.parse_args()
    store = Store(args.db)
    samples = store.adjudicated_samples()
    report = score_decisions(samples)
    payload = asdict(report)
    payload["decision_health"] = store.decision_health()
    print(json.dumps(payload, indent=2, sort_keys=True))


def replay_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("events", type=Path, help="JSONL of normalized SignalEvent-like records")
    args = parser.parse_args()
    events: list[SignalEvent] = []
    for line in args.events.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        row["kind"] = EventKind(row["kind"])
        row["source_ts_utc"] = datetime.fromisoformat(row["source_ts_utc"]).astimezone(UTC)
        row["received_ts_utc"] = datetime.fromisoformat(row["received_ts_utc"]).astimezone(UTC)
        events.append(SignalEvent(**row))
    state, effects = replay(events)
    print(
        json.dumps(
            {
                "halted": state.halted,
                "positions": {key: value.status.value for key, value in state.positions.items()},
                "effects": [effect.kind.value for effect in effects],
                "state_fingerprint": state_fingerprint(state),
            },
            indent=2,
            sort_keys=True,
        )
    )

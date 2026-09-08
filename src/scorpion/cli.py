from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .accuracy import score_decisions
from .decision_store import unresolved_packet_count
from .domain import EventKind, SignalEvent
from .integrity import IntegrityLedger
from .ops_queue import load_operator_inbox
from .policy_bundle import RuntimePolicyBundle
from .provenance import canonical_json
from .reconciliation import ExternalPositionObservation, reconcile_positions
from .recovery import create_verified_backup
from .replay import replay, state_fingerprint
from .resilience import assess_resilience
from .schema_contract import inspect_schema
from .stage_trace import load_stage_latency_report, storage_snapshot
from .storage_health import checkpoint_wal, evaluate_storage_health, inspect_storage
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


def storage_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scorpion.db")
    parser.add_argument(
        "--checkpoint",
        choices=("PASSIVE", "FULL", "RESTART", "TRUNCATE"),
        default=None,
    )
    args = parser.parse_args()
    snapshot = inspect_storage(args.db)
    payload: dict[str, object] = {
        "snapshot": asdict(snapshot),
        "failures": evaluate_storage_health(snapshot),
    }
    if args.checkpoint is not None:
        payload["checkpoint"] = asdict(checkpoint_wal(args.db, args.checkpoint))
        payload["snapshot_after"] = asdict(inspect_storage(args.db))
    print(json.dumps(payload, indent=2, sort_keys=True))


def schema_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scorpion.db")
    args = parser.parse_args()
    print(json.dumps(asdict(inspect_schema(args.db)), indent=2, sort_keys=True))


def backup_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--db", default="scorpion.db")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    report = create_verified_backup(args.db, args.output, overwrite=args.overwrite)
    print(json.dumps(asdict(report), indent=2, sort_keys=True))


def policy_main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    policy = RuntimePolicyBundle()
    payload = json.loads(canonical_json(policy))
    if not isinstance(payload, dict):
        raise RuntimeError("canonical policy payload must be an object")
    payload["fingerprint"] = policy.fingerprint
    print(json.dumps(payload, indent=2, sort_keys=True))


def ops_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scorpion.db")
    parser.add_argument("--latency-window", type=int, default=500)
    parser.add_argument("--queue-limit", type=int, default=20)
    args = parser.parse_args()
    store = Store(args.db)
    health = store.health_snapshot()
    storage = storage_snapshot(args.db)
    resilience = assess_resilience(health, wal_bytes=storage.wal_bytes)
    latency = load_stage_latency_report(args.db, limit=args.latency_window)
    integrity = IntegrityLedger(args.db).verify_database()
    inbox = load_operator_inbox(args.db, limit=args.queue_limit)
    schema = inspect_schema(args.db)
    policy = RuntimePolicyBundle()
    payload = {
        "operational_mode": resilience.mode.value,
        "policy_fingerprint": policy.fingerprint,
        "schema": asdict(schema),
        "resilience_signals": [
            {
                "code": signal.code,
                "severity": signal.severity.value,
                "detail": signal.detail,
            }
            for signal in resilience.signals
        ],
        "recommended_actions": list(resilience.recommended_actions),
        "unresolved_decision_packets": unresolved_packet_count(args.db),
        "operator_inbox": [
            {
                "packet_id": item.packet_id,
                "event_id": item.event_id,
                "event_kind": item.event_kind,
                "contract_key": item.contract_key,
                "disposition": item.disposition,
                "system_mode": item.system_mode,
                "evidence_strength": item.evidence_strength,
                "age_seconds": item.age_seconds,
                "stale": item.stale,
                "priority": item.priority.name,
                "reason_codes": list(item.reason_codes),
            }
            for item in inbox
        ],
        "decision_health": health["decision_health"],
        "pending_raw_revisions": health["pending_raw_revisions"],
        "pending_review_effects": health["pending_review_effects"],
        "stage_latency": asdict(latency),
        "storage": asdict(storage),
        "integrity": asdict(integrity),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def reconcile_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("observations", type=Path, help="JSON array of external positions")
    parser.add_argument("--db", default="scorpion.db")
    args = parser.parse_args()
    rows = json.loads(args.observations.read_text())
    if not isinstance(rows, list):
        raise ValueError("observations must be a JSON array")
    observations = [
        ExternalPositionObservation(
            contract_key=str(row["contract_key"]),
            quantity=int(row["quantity"]),
            average_price=(
                Decimal(str(row["average_price"]))
                if row.get("average_price") is not None
                else None
            ),
        )
        for row in rows
        if isinstance(row, dict)
    ]
    store = Store(args.db)
    state, _ = replay(store.load_signals())
    report = reconcile_positions(state, observations)
    payload = {
        "clean": report.clean,
        "critical": report.critical,
        "findings": [
            {
                "code": finding.code,
                "severity": finding.severity.value,
                "contract_key": finding.contract_key,
                "detail": finding.detail,
            }
            for finding in report.findings
        ],
    }
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

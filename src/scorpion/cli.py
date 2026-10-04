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
from .execution_journal import ExecutionJournal, FillSource, reconstruct_execution_state
from .integrity import IntegrityLedger
from .ops_queue import load_operator_inbox
from .reconciliation import ExternalPositionObservation, reconcile_positions
from .replay import replay, state_fingerprint
from .resilience import assess_resilience
from .stage_trace import load_stage_latency_report, storage_snapshot
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
    execution_truth = reconstruct_execution_state(args.db, store.load_signals())
    inbox = load_operator_inbox(args.db, limit=args.queue_limit)
    payload = {
        "operational_mode": resilience.mode.value,
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
        "execution_truth": {
            "available": execution_truth.available,
            "fills_applied": execution_truth.fills_applied,
            "journal_integrity": asdict(execution_truth.verification),
            "anomalies": list(execution_truth.anomalies),
            "positions": (
                {
                    key: {
                        "status": position.status.value,
                        "quantity": position.quantity,
                        "average_price": str(position.average_price),
                        "generation": position.generation,
                    }
                    for key, position in sorted(execution_truth.state.positions.items())
                }
                if execution_truth.state is not None
                else None
            ),
        },
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
    execution_truth = reconstruct_execution_state(args.db, store.load_signals())
    if not execution_truth.available or execution_truth.state is None:
        payload = {
            "clean": False,
            "critical": True,
            "execution_truth_available": False,
            "fills_applied": execution_truth.fills_applied,
            "journal_integrity": asdict(execution_truth.verification),
            "findings": [
                {
                    "code": "execution_truth_unavailable",
                    "severity": "CRITICAL",
                    "contract_key": "",
                    "detail": ";".join(
                        (*execution_truth.verification.failures, *execution_truth.anomalies)
                    )
                    or "execution state could not be reconstructed safely",
                }
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    report = reconcile_positions(execution_truth.state, observations)
    payload = {
        "clean": report.clean,
        "critical": report.critical,
        "execution_truth_available": True,
        "fills_applied": execution_truth.fills_applied,
        "journal_integrity": asdict(execution_truth.verification),
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



def record_fill_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Record an already-executed paper or externally confirmed fill. "
            "This command never submits an order."
        )
    )
    parser.add_argument("event_id")
    parser.add_argument("--db", default="scorpion.db")
    parser.add_argument("--quantity-delta", type=int, required=True)
    parser.add_argument("--fill-price", type=Decimal, required=True)
    parser.add_argument(
        "--source",
        choices=[source.value for source in FillSource],
        required=True,
    )
    parser.add_argument("--recorded-by", required=True)
    parser.add_argument("--filled-ts")
    parser.add_argument("--external-ref")
    parser.add_argument("--note", default="")
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--fill-id")
    args = parser.parse_args()
    if args.filled_ts:
        parsed_fill_time = datetime.fromisoformat(args.filled_ts)
        if parsed_fill_time.tzinfo is None or parsed_fill_time.utcoffset() is None:
            raise ValueError("--filled-ts must include a timezone offset")
        filled_ts = parsed_fill_time.astimezone(UTC)
    else:
        filled_ts = datetime.now(UTC)
    record = ExecutionJournal(args.db).record(
        args.event_id,
        quantity_delta=args.quantity_delta,
        fill_price=args.fill_price,
        source=FillSource(args.source),
        recorded_by=args.recorded_by,
        filled_ts_utc=filled_ts,
        external_ref=args.external_ref,
        note=args.note,
        final=args.final,
        fill_id=args.fill_id,
    )
    print(
        json.dumps(
            {
                "sequence": record.sequence,
                "fill_id": record.fill.fill_id,
                "event_id": record.fill.event_id,
                "contract_key": record.fill.contract_key,
                "generation": record.fill.generation,
                "quantity_delta": record.fill.quantity_delta,
                "fill_price": str(record.fill.fill_price),
                "source": record.fill.source.value,
                "external_ref": record.fill.external_ref,
                "filled_ts_utc": record.fill.filled_ts_utc.isoformat(),
                "record_hash": record.record_hash,
                "note": "fill recorded; no order was submitted by Scorpion",
            },
            indent=2,
            sort_keys=True,
        )
    )

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

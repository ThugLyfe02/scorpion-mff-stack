from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_CONTRACT_VERSION = "2026.09.v4"

_REQUIRED: dict[str, frozenset[str]] = {
    "raw_discord_events": frozenset(
        {"raw_event_id", "message_id", "channel_id", "author_id", "content_sha256"}
    ),
    "raw_processing": frozenset({"raw_event_id", "status", "updated_ts_utc"}),
    "signal_events": frozenset(
        {"event_id", "message_id", "kind", "contract_key", "parser_version", "payload_json"}
    ),
    "proposed_effects": frozenset(
        {"source_event_id", "kind", "generation", "status", "metadata_json"}
    ),
    "decision_audit": frozenset(
        {"event_id", "parser_rule", "parser_confidence", "association_method"}
    ),
    "heartbeats": frozenset({"component", "last_seen_ts_utc", "metadata_json"}),
}

_OPTIONAL_ADVANCED: dict[str, frozenset[str]] = {
    "operator_decision_packets": frozenset(
        {"packet_id", "event_id", "disposition", "payload_json", "created_ts_utc"}
    ),
    "integrity_ledger": frozenset(
        {"sequence", "record_id", "payload_sha256", "payload_json", "record_hash"}
    ),
    "transition_stage_latency": frozenset({"event_id", "raw_revision_id"}),
    "execution_delivery_ledger": frozenset(
        {
            "delivery_id",
            "event_id",
            "effect_kind",
            "generation",
            "state",
            "attempt_count",
            "lease_until_utc",
        }
    ),
    "component_releases": frozenset(
        {
            "release_id",
            "component",
            "artifact_hash",
            "policy_fingerprint",
            "research_manifest_hash",
            "state",
        }
    ),
    "state_checkpoints": frozenset(
        {
            "checkpoint_id",
            "policy_fingerprint",
            "signal_count",
            "last_event_id",
            "integrity_record_hash",
            "state_fingerprint",
            "state_json",
        }
    ),
    "raw_receipt_order": frozenset({"receipt_seq", "raw_event_id"}),
    "event_processing_order": frozenset({"process_seq", "event_id"}),
    "causal_feature_snapshots": frozenset(
        {
            "event_id",
            "feature_set_version",
            "process_seq",
            "feature_json",
            "feature_sha256",
        }
    ),
    "raw_failure_state": frozenset(
        {
            "raw_event_id",
            "attempt_count",
            "state",
            "last_error",
            "updated_ts_utc",
            "requeued_by",
        }
    ),
    "raw_failure_events": frozenset(
        {"failure_id", "raw_event_id", "attempt_number", "error", "occurred_ts_utc"}
    ),
    "adaptive_ensemble_state": frozenset(
        {
            "ensemble_id",
            "model_id",
            "log_weight",
            "cumulative_loss",
            "updates",
            "updated_ts_utc",
        }
    ),
    "evolution_candidates": frozenset(
        {
            "candidate_id",
            "parent_release_id",
            "trigger",
            "code_revision",
            "policy_fingerprint",
            "dataset_fingerprint",
            "feature_set_version",
            "candidate_sha256",
            "status",
            "created_ts_utc",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class TableContract:
    table: str
    present: bool
    missing_columns: tuple[str, ...]
    required: bool

    @property
    def compatible(self) -> bool:
        return self.present and not self.missing_columns


@dataclass(frozen=True, slots=True)
class SchemaContractReport:
    contract_version: str
    tables: tuple[TableContract, ...]
    compatible: bool
    failures: tuple[str, ...]


def _columns(db: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in db.execute(f"PRAGMA table_info({table})"))


def inspect_schema(path: str | Path) -> SchemaContractReport:
    database = Path(path)
    if not database.exists():
        raise FileNotFoundError(database)
    tables: list[TableContract] = []
    failures: list[str] = []
    with sqlite3.connect(str(database)) as db:
        existing = frozenset(
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
        for table, expected in _REQUIRED.items():
            present = table in existing
            actual = _columns(db, table) if present else frozenset()
            missing = tuple(sorted(expected - actual))
            contract = TableContract(table, present, missing, True)
            tables.append(contract)
            if not present:
                failures.append(f"missing_required_table:{table}")
            for column in missing:
                failures.append(f"missing_required_column:{table}.{column}")
        for table, expected in _OPTIONAL_ADVANCED.items():
            present = table in existing
            actual = _columns(db, table) if present else frozenset()
            missing = tuple(sorted(expected - actual)) if present else ()
            contract = TableContract(table, present, missing, False)
            tables.append(contract)
            for column in missing:
                failures.append(f"incompatible_optional_column:{table}.{column}")
    return SchemaContractReport(
        contract_version=SCHEMA_CONTRACT_VERSION,
        tables=tuple(tables),
        compatible=not failures,
        failures=tuple(failures),
    )


def require_compatible_schema(path: str | Path) -> SchemaContractReport:
    report = inspect_schema(path)
    if not report.compatible:
        raise RuntimeError("database schema contract failed: " + ",".join(report.failures))
    return report

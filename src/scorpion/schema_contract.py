from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_CONTRACT_VERSION = "2026.09.v7"

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
            "previous_release_id",
            "state",
        }
    ),
    "component_safety_state": frozenset(
        {
            "component",
            "mode",
            "source_release_id",
            "reason",
            "updated_ts_utc",
            "updated_by",
        }
    ),
    "component_safety_events": frozenset(
        {
            "event_id",
            "component",
            "mode",
            "source_release_id",
            "reason",
            "created_ts_utc",
            "actor",
        }
    ),
    "component_safety_integrity_state": frozenset(
        {"component", "event_count", "head_event_id", "head_chain_hash"}
    ),
    "production_promotion_dossiers": frozenset(
        {
            "dossier_id",
            "component",
            "evidence_hash",
            "policy_hash",
            "status",
            "expires_ts_utc",
            "dossier_json",
            "dossier_sha256",
            "created_ts_utc",
        }
    ),
    "production_promotion_approvals": frozenset(
        {"approval_id", "dossier_id", "role", "operator", "created_ts_utc"}
    ),
    "model_training_runs": frozenset(
        {
            "run_id",
            "model_id",
            "trainer_version",
            "dataset_fingerprint",
            "split_hash",
            "artifact_sha256",
            "status",
            "report_sha256",
        }
    ),
    "shadow_model_releases": frozenset(
        {
            "shadow_release_id",
            "component",
            "training_run_id",
            "artifact_sha256",
            "parent_release_id",
            "state",
        }
    ),
    "challenger_retraining_plans": frozenset(
        {
            "plan_id",
            "parent_release_id",
            "dataset_fingerprint",
            "status",
            "plan_sha256",
            "created_ts_utc",
        }
    ),
    "deployment_rollouts": frozenset(
        {
            "rollout_id",
            "component",
            "candidate_release_id",
            "previous_release_id",
            "active_release_id",
            "dossier_id",
            "authorization_id",
            "authorization_expires_ts_utc",
            "evidence_bundle_hash",
            "preparation_audit_hash",
            "state",
            "rollback_state",
            "generation",
            "created_ts_utc",
            "updated_ts_utc",
        }
    ),
    "deployment_rollbacks": frozenset(
        {
            "rollback_id",
            "rollout_id",
            "target_release_id",
            "state",
            "proposed_ts_utc",
            "authorized_by",
            "applied_by",
            "verified_by",
            "failure_reason",
        }
    ),
    "deployment_rollout_events": frozenset(
        {
            "event_id",
            "rollout_id",
            "seq",
            "from_state",
            "to_state",
            "rollback_state",
            "actor",
            "reason",
            "created_ts_utc",
            "previous_event_hash",
            "event_hash",
        }
    ),
    "deployment_rollout_integrity_state": frozenset(
        {"rollout_id", "event_count", "head_event_hash", "chain_hash"}
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
    "adaptive_learning_journal": frozenset(
        {
            "sequence",
            "ensemble_id",
            "event_id",
            "truth",
            "drift_active",
            "probabilities_json",
            "policy_json",
            "before_snapshot_hash",
            "after_snapshot_hash",
            "previous_record_hash",
            "record_hash",
            "created_ts_utc",
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
    "adjudication_annotations": frozenset(
        {
            "annotation_id",
            "event_id",
            "reviewer_id",
            "label",
            "note",
            "created_ts_utc",
        }
    ),
}

_ADVANCED_GROUPS: dict[str, frozenset[str]] = {
    "safety_latch_integrity": frozenset(
        {
            "component_safety_state",
            "component_safety_events",
            "component_safety_integrity_state",
        }
    ),
    "production_promotion": frozenset(
        {"production_promotion_dossiers", "production_promotion_approvals"}
    ),
    "deployment_rollout": frozenset(
        {
            "deployment_rollouts",
            "deployment_rollbacks",
            "deployment_rollout_events",
            "deployment_rollout_integrity_state",
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
        for group, members in _ADVANCED_GROUPS.items():
            present_members = members & existing
            if present_members and present_members != members:
                for table in sorted(members - existing):
                    failures.append(f"incomplete_advanced_group:{group}:{table}")
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

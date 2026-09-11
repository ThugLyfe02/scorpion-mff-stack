from __future__ import annotations

import sqlite3
from pathlib import Path

from . import _schema_contract_v9 as _v9
from .schema_migrations import (
    TARGET_SCHEMA_VERSION,
    verify_schema_migration_ledger_connection,
)

SCHEMA_CONTRACT_VERSION = TARGET_SCHEMA_VERSION
TableContract = _v9.TableContract
SchemaContractReport = _v9.SchemaContractReport

_REQUIRED = dict(_v9._REQUIRED)
_OPTIONAL_ADVANCED = dict(_v9._OPTIONAL_ADVANCED)
_OPTIONAL_ADVANCED["deployment_rollouts"] = frozenset(
    set(_OPTIONAL_ADVANCED["deployment_rollouts"])
    | {"readiness_activation_snapshot_hash"}
)
_OPTIONAL_ADVANCED["production_readiness_consumptions"] = frozenset(
    set(_OPTIONAL_ADVANCED["production_readiness_consumptions"])
    | {"activation_snapshot_hash"}
)
_OPTIONAL_ADVANCED["readiness_capability_events"] = frozenset(
    {
        "event_id",
        "certificate_id",
        "seq",
        "event_kind",
        "component",
        "candidate_release_id",
        "rollout_id",
        "actor",
        "reason",
        "payload_sha256",
        "deployment_event_hash",
        "created_ts_utc",
        "previous_event_hash",
        "event_hash",
    }
)
_OPTIONAL_ADVANCED["readiness_capability_integrity_state"] = frozenset(
    {"certificate_id", "event_count", "head_event_hash", "chain_hash"}
)
_OPTIONAL_ADVANCED["schema_migration_state"] = frozenset(
    {
        "singleton_id",
        "schema_version",
        "last_migration_id",
        "last_migration_hash",
        "updated_ts_utc",
    }
)
_OPTIONAL_ADVANCED["schema_migration_attempts"] = frozenset(
    {
        "attempt_id",
        "migration_id",
        "from_version",
        "to_version",
        "migration_sha256",
        "application_id",
        "status",
        "started_ts_utc",
        "finished_ts_utc",
        "error_class",
    }
)
_OPTIONAL_ADVANCED["schema_migration_events"] = frozenset(
    {
        "event_id",
        "seq",
        "attempt_id",
        "migration_id",
        "event_kind",
        "from_version",
        "to_version",
        "migration_sha256",
        "application_id",
        "created_ts_utc",
        "previous_event_hash",
        "event_hash",
    }
)
_OPTIONAL_ADVANCED["schema_migration_integrity_state"] = frozenset(
    {"singleton_id", "event_count", "head_event_hash", "chain_hash"}
)

_ADVANCED_GROUPS = dict(_v9._ADVANCED_GROUPS)
_ADVANCED_GROUPS["deployment_rollout"] = frozenset(
    set(_ADVANCED_GROUPS["deployment_rollout"])
    | {"readiness_capability_events", "readiness_capability_integrity_state"}
)
_ADVANCED_GROUPS["schema_migration_provenance"] = frozenset(
    {
        "schema_migration_state",
        "schema_migration_attempts",
        "schema_migration_events",
        "schema_migration_integrity_state",
    }
)


def _columns(db: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in db.execute(f"PRAGMA table_info({table})"))


def inspect_schema_connection(db: sqlite3.Connection) -> SchemaContractReport:
    db.row_factory = sqlite3.Row
    existing = frozenset(
        str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )
    tables: list[TableContract] = []
    failures: list[str] = []
    for table, expected in _REQUIRED.items():
        present = table in existing
        actual = _columns(db, table) if present else frozenset()
        missing = tuple(sorted(expected - actual))
        tables.append(TableContract(table, present, missing, True))
        if not present:
            failures.append(f"missing_required_table:{table}")
        failures.extend(f"missing_required_column:{table}.{column}" for column in missing)
    for table, expected in _OPTIONAL_ADVANCED.items():
        present = table in existing
        actual = _columns(db, table) if present else frozenset()
        missing = tuple(sorted(expected - actual)) if present else ()
        tables.append(TableContract(table, present, missing, False))
        failures.extend(f"incompatible_optional_column:{table}.{column}" for column in missing)
    for group, members in _ADVANCED_GROUPS.items():
        present_members = members & existing
        if present_members and present_members != members:
            failures.extend(
                f"incomplete_advanced_group:{group}:{table}"
                for table in sorted(members - existing)
            )
    migration_members = _ADVANCED_GROUPS["schema_migration_provenance"]
    if "deployment_rollouts" in existing and not migration_members <= existing:
        failures.append("deployment_schema_not_migration_managed")
    if migration_members <= existing:
        migration = verify_schema_migration_ledger_connection(db)
        if not migration.valid:
            failures.extend(f"schema_migration:{failure}" for failure in migration.failures)
        elif migration.current_version != SCHEMA_CONTRACT_VERSION:
            failures.append(
                f"schema_migration_version_mismatch:{migration.current_version}"
            )
    return SchemaContractReport(
        contract_version=SCHEMA_CONTRACT_VERSION,
        tables=tuple(tables),
        compatible=not failures,
        failures=tuple(dict.fromkeys(failures)),
    )


def inspect_schema(path: str | Path) -> SchemaContractReport:
    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(database)
    with sqlite3.connect(str(database)) as db:
        return inspect_schema_connection(db)


def require_compatible_schema(path: str | Path) -> SchemaContractReport:
    report = inspect_schema(path)
    if not report.compatible:
        raise RuntimeError("database schema contract failed: " + ",".join(report.failures))
    return report

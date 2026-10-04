from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from . import _schema_migrations_v27_core as _core

TARGET_SCHEMA_VERSION = _core.TARGET_SCHEMA_VERSION
LEGACY_SCHEMA_VERSION = _core.LEGACY_SCHEMA_VERSION
MIGRATION_ID = _core.MIGRATION_ID
MIGRATION_SHA256 = _core.MIGRATION_SHA256
MigrationLedgerReport = _core.MigrationLedgerReport

verify_schema_migration_ledger_connection = _core.verify_schema_migration_ledger_connection
verify_schema_migration_ledger = _core.verify_schema_migration_ledger

_LEDGER_DDL = (
    """
    CREATE TABLE IF NOT EXISTS schema_migration_state (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
        schema_version TEXT NOT NULL,
        last_migration_id TEXT NOT NULL,
        last_migration_hash TEXT NOT NULL,
        updated_ts_utc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_migration_attempts (
        attempt_id TEXT PRIMARY KEY,
        migration_id TEXT NOT NULL,
        from_version TEXT NOT NULL,
        to_version TEXT NOT NULL,
        migration_sha256 TEXT NOT NULL,
        application_id TEXT NOT NULL,
        status TEXT NOT NULL,
        started_ts_utc TEXT NOT NULL,
        finished_ts_utc TEXT NOT NULL DEFAULT '',
        error_class TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_migration_events (
        event_id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL UNIQUE,
        attempt_id TEXT NOT NULL,
        migration_id TEXT NOT NULL,
        event_kind TEXT NOT NULL,
        from_version TEXT NOT NULL,
        to_version TEXT NOT NULL,
        migration_sha256 TEXT NOT NULL,
        application_id TEXT NOT NULL,
        created_ts_utc TEXT NOT NULL,
        previous_event_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_migration_integrity_state (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
        event_count INTEGER NOT NULL,
        head_event_hash TEXT NOT NULL,
        chain_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS schema_migration_events_no_update
    BEFORE UPDATE ON schema_migration_events
    BEGIN
        SELECT RAISE(ABORT,'schema_migration_events_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS schema_migration_events_no_delete
    BEFORE DELETE ON schema_migration_events
    BEGIN
        SELECT RAISE(ABORT,'schema_migration_events_append_only');
    END
    """,
)

_V10_DDL = (
    """
    CREATE TABLE IF NOT EXISTS production_readiness_consumptions (
        certificate_id TEXT PRIMARY KEY,
        component TEXT NOT NULL,
        candidate_release_id TEXT NOT NULL,
        dossier_id TEXT NOT NULL,
        authorization_id TEXT NOT NULL,
        evidence_bundle_hash TEXT NOT NULL,
        control_state_hash TEXT NOT NULL,
        activation_control_hash TEXT NOT NULL DEFAULT '',
        activation_snapshot_hash TEXT NOT NULL DEFAULT '',
        operator_state_hash TEXT NOT NULL DEFAULT '',
        activation_operator_state_hash TEXT NOT NULL DEFAULT '',
        bottleneck_state_hash TEXT NOT NULL DEFAULT '',
        activation_bottleneck_hash TEXT NOT NULL DEFAULT '',
        bottleneck_policy_json TEXT NOT NULL DEFAULT '',
        bottleneck_policy_sha256 TEXT NOT NULL DEFAULT '',
        safety_event_count INTEGER NOT NULL DEFAULT 0,
        safety_head_event_id TEXT NOT NULL DEFAULT '',
        safety_chain_hash TEXT NOT NULL DEFAULT '',
        certificate_expires_ts_utc TEXT NOT NULL,
        consumed_rollout_id TEXT NOT NULL DEFAULT '',
        consumed_ts_utc TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_readiness_consumed_rollout
    ON production_readiness_consumptions(consumed_rollout_id)
    WHERE consumed_rollout_id<>''
    """,
    """
    CREATE TABLE IF NOT EXISTS readiness_capability_events (
        event_id TEXT PRIMARY KEY,
        certificate_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_kind TEXT NOT NULL,
        component TEXT NOT NULL,
        candidate_release_id TEXT NOT NULL,
        rollout_id TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        deployment_event_hash TEXT NOT NULL DEFAULT '',
        created_ts_utc TEXT NOT NULL,
        previous_event_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL,
        UNIQUE(certificate_id,seq)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_readiness_capability_certificate_seq
    ON readiness_capability_events(certificate_id,seq)
    """,
    """
    CREATE TABLE IF NOT EXISTS readiness_capability_integrity_state (
        certificate_id TEXT PRIMARY KEY,
        event_count INTEGER NOT NULL,
        head_event_hash TEXT NOT NULL,
        chain_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS readiness_capability_events_no_update
    BEFORE UPDATE ON readiness_capability_events
    BEGIN
        SELECT RAISE(ABORT,'readiness_capability_events_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS readiness_capability_events_no_delete
    BEFORE DELETE ON readiness_capability_events
    BEGIN
        SELECT RAISE(ABORT,'readiness_capability_events_append_only');
    END
    """,
)


def _execute_ddl(db: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    """Execute DDL without sqlite3.executescript so the caller owns the transaction."""
    for statement in statements:
        db.execute(statement)


def _ensure_ledger_schema_transactional(db: sqlite3.Connection) -> None:
    _execute_ddl(db, _LEDGER_DDL)


def _apply_v10_transactional(db: sqlite3.Connection) -> None:
    if not _core._table(db, "deployment_rollouts"):
        raise RuntimeError("deployment_rollouts must exist before production migration")
    _execute_ddl(db, _V10_DDL)
    for name, ddl in _core._DEPLOYMENT_COLUMNS:
        _core._column(db, "deployment_rollouts", name, ddl)
    for name, ddl in _core._CONSUMPTION_COLUMNS:
        _core._column(db, "production_readiness_consumptions", name, ddl)


def _rollback_if_active(db: sqlite3.Connection) -> None:
    if db.in_transaction:
        db.execute("ROLLBACK")


def apply_schema_migrations(
    path: str | Path,
    *,
    application_id: str,
    now: datetime | None = None,
) -> MigrationLedgerReport:
    """Apply the production migration while retaining explicit SQLite transaction ownership."""
    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(database)
    if not application_id.strip():
        raise ValueError("application_id is required")
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    db = sqlite3.connect(str(database), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")

        # Bootstrap and reconcile the provenance ledger atomically. Individual execute() calls
        # are intentional: executescript() commits implicitly and would sever this transaction.
        db.execute("BEGIN IMMEDIATE")
        try:
            _ensure_ledger_schema_transactional(db)
            state = db.execute(
                "SELECT * FROM schema_migration_state WHERE singleton_id=1"
            ).fetchone()
            if state is None:
                db.execute(
                    "INSERT INTO schema_migration_state VALUES (1,?,'','',?)",
                    (LEGACY_SCHEMA_VERSION, timestamp.isoformat()),
                )
            stale = db.execute(
                "SELECT * FROM schema_migration_attempts "
                "WHERE status='STARTED' ORDER BY started_ts_utc"
            ).fetchall()
            for row in stale:
                db.execute(
                    "UPDATE schema_migration_attempts "
                    "SET status='INTERRUPTED',finished_ts_utc=? "
                    "WHERE attempt_id=? AND status='STARTED'",
                    (timestamp.isoformat(), str(row["attempt_id"])),
                )
                _core._append(
                    db,
                    attempt_id=str(row["attempt_id"]),
                    kind="INTERRUPTED",
                    from_version=str(row["from_version"]),
                    application_id=application_id,
                    when=timestamp,
                )
            db.execute("COMMIT")
        except Exception:
            _rollback_if_active(db)
            raise

        state = db.execute(
            "SELECT * FROM schema_migration_state WHERE singleton_id=1"
        ).fetchone()
        if state is None:
            raise RuntimeError("schema migration state disappeared after bootstrap")
        current = str(state["schema_version"])
        if current == TARGET_SCHEMA_VERSION:
            report = verify_schema_migration_ledger_connection(db)
            if not report.valid:
                raise RuntimeError(
                    "schema migration ledger invalid: " + ",".join(report.failures)
                )
            return report
        if current != LEGACY_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {current!r} is unsupported by code targeting "
                f"{TARGET_SCHEMA_VERSION!r}"
            )

        attempt_id = _core._hash(
            {
                "version": "schema-migration-attempt-v1",
                "migration_id": MIGRATION_ID,
                "application_id": application_id,
                "started_ts_utc": timestamp.isoformat(),
            }
        )
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """
                INSERT INTO schema_migration_attempts
                (attempt_id,migration_id,from_version,to_version,migration_sha256,application_id,
                 status,started_ts_utc)
                VALUES (?,?,?,?,?,?,'STARTED',?)
                """,
                (
                    attempt_id,
                    MIGRATION_ID,
                    current,
                    TARGET_SCHEMA_VERSION,
                    MIGRATION_SHA256,
                    application_id,
                    timestamp.isoformat(),
                ),
            )
            _core._append(
                db,
                attempt_id=attempt_id,
                kind="STARTED",
                from_version=current,
                application_id=application_id,
                when=timestamp,
            )
            db.execute("COMMIT")
        except Exception:
            _rollback_if_active(db)
            raise

        try:
            db.execute("BEGIN EXCLUSIVE")
            _apply_v10_transactional(db)
            postconditions = _core._postconditions(db)
            if postconditions:
                raise RuntimeError(";".join(postconditions))
            applied_hash = _core._append(
                db,
                attempt_id=attempt_id,
                kind="APPLIED",
                from_version=current,
                application_id=application_id,
                when=timestamp,
            )
            db.execute(
                "UPDATE schema_migration_attempts "
                "SET status='APPLIED',finished_ts_utc=? WHERE attempt_id=?",
                (timestamp.isoformat(), attempt_id),
            )
            db.execute(
                """
                UPDATE schema_migration_state
                SET schema_version=?,last_migration_id=?,last_migration_hash=?,updated_ts_utc=?
                WHERE singleton_id=1
                """,
                (
                    TARGET_SCHEMA_VERSION,
                    MIGRATION_ID,
                    applied_hash,
                    timestamp.isoformat(),
                ),
            )
            db.execute("COMMIT")
        except Exception as exc:
            _rollback_if_active(db)
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "UPDATE schema_migration_attempts "
                    "SET status='FAILED',finished_ts_utc=?,error_class=? "
                    "WHERE attempt_id=?",
                    (timestamp.isoformat(), type(exc).__name__, attempt_id),
                )
                _core._append(
                    db,
                    attempt_id=attempt_id,
                    kind="FAILED",
                    from_version=current,
                    application_id=application_id,
                    when=timestamp,
                )
                db.execute("COMMIT")
            except Exception:
                _rollback_if_active(db)
                raise
            raise RuntimeError("schema migration failed") from exc

        report = verify_schema_migration_ledger_connection(db)
        if not report.valid:
            raise RuntimeError(
                "schema migration verification failed: " + ",".join(report.failures)
            )
        return report
    finally:
        db.close()

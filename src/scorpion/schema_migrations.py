from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

TARGET_SCHEMA_VERSION = "2026.09.v10"
LEGACY_SCHEMA_VERSION = "legacy-untracked"
MIGRATION_ID = "2026.09.v10-control-plane-provenance"


@dataclass(frozen=True, slots=True)
class MigrationLedgerReport:
    current_version: str
    events: int
    head_event_hash: str
    chain_hash: str
    valid: bool
    failures: tuple[str, ...]


_DEPLOYMENT_COLUMNS = (
    ("readiness_certificate_id", "readiness_certificate_id TEXT NOT NULL DEFAULT ''"),
    ("readiness_expires_ts_utc", "readiness_expires_ts_utc TEXT NOT NULL DEFAULT ''"),
    ("readiness_operator_state_hash", "readiness_operator_state_hash TEXT NOT NULL DEFAULT ''"),
    (
        "readiness_activation_operator_state_hash",
        "readiness_activation_operator_state_hash TEXT NOT NULL DEFAULT ''",
    ),
    ("readiness_bottleneck_state_hash", "readiness_bottleneck_state_hash TEXT NOT NULL DEFAULT ''"),
    (
        "readiness_activation_bottleneck_hash",
        "readiness_activation_bottleneck_hash TEXT NOT NULL DEFAULT ''",
    ),
    ("readiness_control_state_hash", "readiness_control_state_hash TEXT NOT NULL DEFAULT ''"),
    (
        "readiness_activation_control_hash",
        "readiness_activation_control_hash TEXT NOT NULL DEFAULT ''",
    ),
    (
        "readiness_activation_snapshot_hash",
        "readiness_activation_snapshot_hash TEXT NOT NULL DEFAULT ''",
    ),
    (
        "readiness_bottleneck_policy_hash",
        "readiness_bottleneck_policy_hash TEXT NOT NULL DEFAULT ''",
    ),
)

_CONSUMPTION_COLUMNS = (
    ("activation_control_hash", "activation_control_hash TEXT NOT NULL DEFAULT ''"),
    ("activation_snapshot_hash", "activation_snapshot_hash TEXT NOT NULL DEFAULT ''"),
    ("operator_state_hash", "operator_state_hash TEXT NOT NULL DEFAULT ''"),
    (
        "activation_operator_state_hash",
        "activation_operator_state_hash TEXT NOT NULL DEFAULT ''",
    ),
    ("bottleneck_state_hash", "bottleneck_state_hash TEXT NOT NULL DEFAULT ''"),
    ("activation_bottleneck_hash", "activation_bottleneck_hash TEXT NOT NULL DEFAULT ''"),
    ("bottleneck_policy_json", "bottleneck_policy_json TEXT NOT NULL DEFAULT ''"),
    ("bottleneck_policy_sha256", "bottleneck_policy_sha256 TEXT NOT NULL DEFAULT ''"),
    ("safety_event_count", "safety_event_count INTEGER NOT NULL DEFAULT 0"),
    ("safety_head_event_id", "safety_head_event_id TEXT NOT NULL DEFAULT ''"),
    ("safety_chain_hash", "safety_chain_hash TEXT NOT NULL DEFAULT ''"),
)

_MANIFEST = {
    "id": MIGRATION_ID,
    "from": LEGACY_SCHEMA_VERSION,
    "to": TARGET_SCHEMA_VERSION,
    "deployment_columns": _DEPLOYMENT_COLUMNS,
    "consumption_columns": _CONSUMPTION_COLUMNS,
    "features": (
        "transactional-ddl",
        "checksummed-migration-ledger",
        "append-only-readiness-journal",
        "single-snapshot-activation",
    ),
}
MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(_MANIFEST, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def _hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _table(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def _columns(db: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in db.execute(f"PRAGMA table_info({table})"))


def _column(db: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _ensure_ledger_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migration_state (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
            schema_version TEXT NOT NULL,last_migration_id TEXT NOT NULL,
            last_migration_hash TEXT NOT NULL,updated_ts_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schema_migration_attempts (
            attempt_id TEXT PRIMARY KEY,migration_id TEXT NOT NULL,
            from_version TEXT NOT NULL,to_version TEXT NOT NULL,
            migration_sha256 TEXT NOT NULL,application_id TEXT NOT NULL,
            status TEXT NOT NULL,started_ts_utc TEXT NOT NULL,
            finished_ts_utc TEXT NOT NULL DEFAULT '',error_class TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS schema_migration_events (
            event_id TEXT PRIMARY KEY,seq INTEGER NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL,migration_id TEXT NOT NULL,event_kind TEXT NOT NULL,
            from_version TEXT NOT NULL,to_version TEXT NOT NULL,
            migration_sha256 TEXT NOT NULL,application_id TEXT NOT NULL,
            created_ts_utc TEXT NOT NULL,previous_event_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schema_migration_integrity_state (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),event_count INTEGER NOT NULL,
            head_event_hash TEXT NOT NULL,chain_hash TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS schema_migration_events_no_update
        BEFORE UPDATE ON schema_migration_events
        BEGIN SELECT RAISE(ABORT,'schema_migration_events_append_only'); END;
        CREATE TRIGGER IF NOT EXISTS schema_migration_events_no_delete
        BEFORE DELETE ON schema_migration_events
        BEGIN SELECT RAISE(ABORT,'schema_migration_events_append_only'); END;
        """
    )


def _event_hash(
    *,
    seq: int,
    attempt_id: str,
    kind: str,
    from_version: str,
    application_id: str,
    when: datetime,
    previous_hash: str,
) -> str:
    return _hash(
        {
            "version": "schema-migration-event-v1",
            "seq": seq,
            "attempt_id": attempt_id,
            "migration_id": MIGRATION_ID,
            "event_kind": kind,
            "from_version": from_version,
            "to_version": TARGET_SCHEMA_VERSION,
            "migration_sha256": MIGRATION_SHA256,
            "application_id": application_id,
            "created_ts_utc": when.astimezone(UTC).isoformat(),
            "previous_event_hash": previous_hash,
        }
    )


def _append(
    db: sqlite3.Connection,
    *,
    attempt_id: str,
    kind: str,
    from_version: str,
    application_id: str,
    when: datetime,
) -> str:
    db.row_factory = sqlite3.Row
    last = db.execute(
        "SELECT seq,event_hash FROM schema_migration_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    seq = int(last["seq"]) + 1 if last is not None else 1
    previous = str(last["event_hash"]) if last is not None else ""
    digest = _event_hash(
        seq=seq,
        attempt_id=attempt_id,
        kind=kind,
        from_version=from_version,
        application_id=application_id,
        when=when,
        previous_hash=previous,
    )
    event_id = _hash(
        {"version": "schema-migration-event-id-v1", "seq": seq, "hash": digest}
    )
    db.execute(
        """
        INSERT INTO schema_migration_events
        (event_id,seq,attempt_id,migration_id,event_kind,from_version,to_version,
         migration_sha256,application_id,created_ts_utc,previous_event_hash,event_hash)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            seq,
            attempt_id,
            MIGRATION_ID,
            kind,
            from_version,
            TARGET_SCHEMA_VERSION,
            MIGRATION_SHA256,
            application_id,
            when.astimezone(UTC).isoformat(),
            previous,
            digest,
        ),
    )
    checkpoint = db.execute(
        "SELECT event_count,chain_hash FROM schema_migration_integrity_state WHERE singleton_id=1"
    ).fetchone()
    count = int(checkpoint["event_count"]) if checkpoint is not None else 0
    chain = str(checkpoint["chain_hash"]) if checkpoint is not None else ""
    chain = hashlib.sha256(f"{chain}|{event_id}".encode()).hexdigest()
    db.execute(
        """
        INSERT INTO schema_migration_integrity_state
        (singleton_id,event_count,head_event_hash,chain_hash) VALUES (1,?,?,?)
        ON CONFLICT(singleton_id) DO UPDATE SET event_count=excluded.event_count,
        head_event_hash=excluded.head_event_hash,chain_hash=excluded.chain_hash
        """,
        (count + 1, digest, chain),
    )
    return digest


def _apply_v10(db: sqlite3.Connection) -> None:
    if not _table(db, "deployment_rollouts"):
        raise RuntimeError("deployment_rollouts must exist before production migration")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS production_readiness_consumptions (
            certificate_id TEXT PRIMARY KEY,component TEXT NOT NULL,
            candidate_release_id TEXT NOT NULL,dossier_id TEXT NOT NULL,
            authorization_id TEXT NOT NULL,evidence_bundle_hash TEXT NOT NULL,
            control_state_hash TEXT NOT NULL,activation_control_hash TEXT NOT NULL DEFAULT '',
            activation_snapshot_hash TEXT NOT NULL DEFAULT '',
            operator_state_hash TEXT NOT NULL DEFAULT '',
            activation_operator_state_hash TEXT NOT NULL DEFAULT '',
            bottleneck_state_hash TEXT NOT NULL DEFAULT '',
            activation_bottleneck_hash TEXT NOT NULL DEFAULT '',
            bottleneck_policy_json TEXT NOT NULL DEFAULT '',
            bottleneck_policy_sha256 TEXT NOT NULL DEFAULT '',
            safety_event_count INTEGER NOT NULL DEFAULT 0,
            safety_head_event_id TEXT NOT NULL DEFAULT '',
            safety_chain_hash TEXT NOT NULL DEFAULT '',certificate_expires_ts_utc TEXT NOT NULL,
            consumed_rollout_id TEXT NOT NULL DEFAULT '',consumed_ts_utc TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_readiness_consumed_rollout
        ON production_readiness_consumptions(consumed_rollout_id) WHERE consumed_rollout_id<>'';
        CREATE TABLE IF NOT EXISTS readiness_capability_events (
            event_id TEXT PRIMARY KEY,certificate_id TEXT NOT NULL,seq INTEGER NOT NULL,
            event_kind TEXT NOT NULL,component TEXT NOT NULL,candidate_release_id TEXT NOT NULL,
            rollout_id TEXT NOT NULL DEFAULT '',actor TEXT NOT NULL,reason TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,deployment_event_hash TEXT NOT NULL DEFAULT '',
            created_ts_utc TEXT NOT NULL,previous_event_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,UNIQUE(certificate_id,seq)
        );
        CREATE INDEX IF NOT EXISTS idx_readiness_capability_certificate_seq
        ON readiness_capability_events(certificate_id,seq);
        CREATE TABLE IF NOT EXISTS readiness_capability_integrity_state (
            certificate_id TEXT PRIMARY KEY,event_count INTEGER NOT NULL,
            head_event_hash TEXT NOT NULL,chain_hash TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS readiness_capability_events_no_update
        BEFORE UPDATE ON readiness_capability_events
        BEGIN SELECT RAISE(ABORT,'readiness_capability_events_append_only'); END;
        CREATE TRIGGER IF NOT EXISTS readiness_capability_events_no_delete
        BEFORE DELETE ON readiness_capability_events
        BEGIN SELECT RAISE(ABORT,'readiness_capability_events_append_only'); END;
        """
    )
    for name, ddl in _DEPLOYMENT_COLUMNS:
        _column(db, "deployment_rollouts", name, ddl)
    for name, ddl in _CONSUMPTION_COLUMNS:
        _column(db, "production_readiness_consumptions", name, ddl)


def _postconditions(db: sqlite3.Connection) -> tuple[str, ...]:
    failures: list[str] = []
    for table in (
        "deployment_rollouts",
        "production_readiness_consumptions",
        "readiness_capability_events",
        "readiness_capability_integrity_state",
    ):
        if not _table(db, table):
            failures.append(f"migration_table_missing:{table}")
    if _table(db, "deployment_rollouts"):
        failures.extend(
            f"migration_column_missing:deployment_rollouts.{name}"
            for name, _ in _DEPLOYMENT_COLUMNS
            if name not in _columns(db, "deployment_rollouts")
        )
    if _table(db, "production_readiness_consumptions"):
        failures.extend(
            f"migration_column_missing:production_readiness_consumptions.{name}"
            for name, _ in _CONSUMPTION_COLUMNS
            if name not in _columns(db, "production_readiness_consumptions")
        )
    return tuple(failures)


def verify_schema_migration_ledger_connection(db: sqlite3.Connection) -> MigrationLedgerReport:
    db.row_factory = sqlite3.Row
    required = (
        "schema_migration_state",
        "schema_migration_events",
        "schema_migration_integrity_state",
    )
    if not all(_table(db, table) for table in required):
        return MigrationLedgerReport("", 0, "", "", False, ("schema_migration_ledger_missing",))
    state = db.execute("SELECT * FROM schema_migration_state WHERE singleton_id=1").fetchone()
    current = str(state["schema_version"]) if state is not None else ""
    failures: list[str] = []
    if current not in {LEGACY_SCHEMA_VERSION, TARGET_SCHEMA_VERSION}:
        failures.append(f"unsupported_schema_version:{current}")
    rows = db.execute("SELECT * FROM schema_migration_events ORDER BY seq").fetchall()
    previous = ""
    chain = ""
    for index, row in enumerate(rows, start=1):
        created = datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC)
        expected = _event_hash(
            seq=int(row["seq"]),
            attempt_id=str(row["attempt_id"]),
            kind=str(row["event_kind"]),
            from_version=str(row["from_version"]),
            application_id=str(row["application_id"]),
            when=created,
            previous_hash=str(row["previous_event_hash"]),
        )
        if int(row["seq"]) != index:
            failures.append("schema_migration_event_sequence_gap")
        if str(row["previous_event_hash"]) != previous or str(row["event_hash"]) != expected:
            failures.append("schema_migration_event_hash_mismatch")
        identity_valid = (
            str(row["migration_id"]) == MIGRATION_ID
            and str(row["to_version"]) == TARGET_SCHEMA_VERSION
            and str(row["migration_sha256"]) == MIGRATION_SHA256
        )
        if not identity_valid:
            failures.append("schema_migration_identity_or_checksum_mismatch")
        expected_id = _hash(
            {
                "version": "schema-migration-event-id-v1",
                "seq": int(row["seq"]),
                "hash": str(row["event_hash"]),
            }
        )
        if str(row["event_id"]) != expected_id:
            failures.append("schema_migration_event_id_mismatch")
        chain = hashlib.sha256(f"{chain}|{row['event_id']}".encode()).hexdigest()
        previous = str(row["event_hash"])
    checkpoint = db.execute(
        "SELECT * FROM schema_migration_integrity_state WHERE singleton_id=1"
    ).fetchone()
    checkpoint_valid = checkpoint is not None and (
        int(checkpoint["event_count"]) == len(rows)
        and str(checkpoint["head_event_hash"]) == previous
        and str(checkpoint["chain_hash"]) == chain
    )
    if not checkpoint_valid:
        failures.append("schema_migration_integrity_checkpoint_mismatch")
    if current == TARGET_SCHEMA_VERSION:
        applied = [row for row in rows if str(row["event_kind"]) == "APPLIED"]
        applied_hash = str(applied[-1]["event_hash"]) if applied else ""
        state_invalid = (
            state is None
            or str(state["last_migration_id"]) != MIGRATION_ID
            or str(state["last_migration_hash"]) != applied_hash
        )
        if state_invalid:
            failures.append("schema_migration_state_mismatch")
        failures.extend(_postconditions(db))
    unique_failures = tuple(dict.fromkeys(failures))
    return MigrationLedgerReport(
        current_version=current,
        events=len(rows),
        head_event_hash=previous,
        chain_hash=chain,
        valid=not unique_failures,
        failures=unique_failures,
    )


def verify_schema_migration_ledger(path: str | Path) -> MigrationLedgerReport:
    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(database)
    with sqlite3.connect(str(database)) as db:
        db.row_factory = sqlite3.Row
        return verify_schema_migration_ledger_connection(db)


def apply_schema_migrations(
    path: str | Path,
    *,
    application_id: str,
    now: datetime | None = None,
) -> MigrationLedgerReport:
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
        db.execute("BEGIN IMMEDIATE")
        _ensure_ledger_schema(db)
        state = db.execute("SELECT * FROM schema_migration_state WHERE singleton_id=1").fetchone()
        if state is None:
            db.execute(
                "INSERT INTO schema_migration_state VALUES (1,?,'','',?)",
                (LEGACY_SCHEMA_VERSION, timestamp.isoformat()),
            )
        stale = db.execute(
            "SELECT * FROM schema_migration_attempts WHERE status='STARTED' ORDER BY started_ts_utc"
        ).fetchall()
        for row in stale:
            db.execute(
                "UPDATE schema_migration_attempts SET status='INTERRUPTED',finished_ts_utc=? "
                "WHERE attempt_id=? AND status='STARTED'",
                (timestamp.isoformat(), str(row["attempt_id"])),
            )
            _append(
                db,
                attempt_id=str(row["attempt_id"]),
                kind="INTERRUPTED",
                from_version=str(row["from_version"]),
                application_id=application_id,
                when=timestamp,
            )
        db.execute("COMMIT")
        state = db.execute("SELECT * FROM schema_migration_state WHERE singleton_id=1").fetchone()
        current = str(state["schema_version"])
        if current == TARGET_SCHEMA_VERSION:
            report = verify_schema_migration_ledger_connection(db)
            if not report.valid:
                raise RuntimeError("schema migration ledger invalid: " + ",".join(report.failures))
            return report
        if current != LEGACY_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {current!r} is unsupported by code targeting "
                f"{TARGET_SCHEMA_VERSION!r}"
            )
        attempt_id = _hash(
            {
                "version": "schema-migration-attempt-v1",
                "migration_id": MIGRATION_ID,
                "application_id": application_id,
                "started_ts_utc": timestamp.isoformat(),
            }
        )
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            INSERT INTO schema_migration_attempts
            (attempt_id,migration_id,from_version,to_version,migration_sha256,application_id,
             status,started_ts_utc) VALUES (?,?,?,?,?,?,'STARTED',?)
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
        _append(
            db,
            attempt_id=attempt_id,
            kind="STARTED",
            from_version=current,
            application_id=application_id,
            when=timestamp,
        )
        db.execute("COMMIT")
        try:
            db.execute("BEGIN EXCLUSIVE")
            _apply_v10(db)
            post = _postconditions(db)
            if post:
                raise RuntimeError(";".join(post))
            applied_hash = _append(
                db,
                attempt_id=attempt_id,
                kind="APPLIED",
                from_version=current,
                application_id=application_id,
                when=timestamp,
            )
            db.execute(
                "UPDATE schema_migration_attempts SET status='APPLIED',finished_ts_utc=? "
                "WHERE attempt_id=?",
                (timestamp.isoformat(), attempt_id),
            )
            db.execute(
                """
                UPDATE schema_migration_state SET schema_version=?,last_migration_id=?,
                last_migration_hash=?,updated_ts_utc=? WHERE singleton_id=1
                """,
                (TARGET_SCHEMA_VERSION, MIGRATION_ID, applied_hash, timestamp.isoformat()),
            )
            db.execute("COMMIT")
        except Exception as exc:
            db.execute("ROLLBACK")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE schema_migration_attempts SET status='FAILED',finished_ts_utc=?,"
                "error_class=? WHERE attempt_id=?",
                (timestamp.isoformat(), type(exc).__name__, attempt_id),
            )
            _append(
                db,
                attempt_id=attempt_id,
                kind="FAILED",
                from_version=current,
                application_id=application_id,
                when=timestamp,
            )
            db.execute("COMMIT")
            raise RuntimeError("schema migration failed") from exc
        report = verify_schema_migration_ledger_connection(db)
        if not report.valid:
            raise RuntimeError("schema migration verification failed: " + ",".join(report.failures))
        return report
    finally:
        db.close()

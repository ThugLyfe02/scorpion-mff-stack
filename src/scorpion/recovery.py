from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .integrity import IntegrityLedger
from .policy_bundle import RuntimePolicyBundle
from .processing_order import inspect_processing_order, load_signals_in_processing_order
from .provenance import FileFingerprint, fingerprint_file
from .replay import ReplayOrder, replay, state_fingerprint


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    signal_count: int
    state_fingerprint: str
    integrity_head: str
    database_evidence_ok: bool
    policy_fingerprint: str
    process_order_fingerprint: str
    processing_order_complete: bool


@dataclass(frozen=True, slots=True)
class BackupVerification:
    source: RecoverySnapshot
    backup: RecoverySnapshot
    backup_file: FileFingerprint
    sqlite_integrity: str
    foreign_key_violations: int
    verified: bool
    failures: tuple[str, ...]


def _snapshot(
    path: str | Path,
    *,
    runtime_policy: RuntimePolicyBundle,
) -> RecoverySnapshot:
    signals = load_signals_in_processing_order(path)
    state, _ = replay(
        signals,
        runtime_policy.base,
        order=ReplayOrder.INPUT,
    )
    ledger = IntegrityLedger(path)
    evidence = ledger.verify_database()
    processing_order = inspect_processing_order(path)
    return RecoverySnapshot(
        signal_count=len(signals),
        state_fingerprint=state_fingerprint(state),
        integrity_head=ledger.head_hash(),
        database_evidence_ok=evidence.ok,
        policy_fingerprint=runtime_policy.fingerprint,
        process_order_fingerprint=processing_order.process_fingerprint,
        processing_order_complete=processing_order.complete,
    )


def _database_checks(path: str | Path) -> tuple[str, int]:
    with sqlite3.connect(str(path)) as db:
        integrity_row = db.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0]) if integrity_row is not None else "missing"
        violations = len(db.execute("PRAGMA foreign_key_check").fetchall())
    return integrity, violations


def verify_backup(
    source_path: str | Path,
    backup_path: str | Path,
    *,
    runtime_policy: RuntimePolicyBundle | None = None,
) -> BackupVerification:
    policy = runtime_policy or RuntimePolicyBundle()
    source = _snapshot(source_path, runtime_policy=policy)
    backup = _snapshot(backup_path, runtime_policy=policy)
    sqlite_integrity, foreign_key_violations = _database_checks(backup_path)
    failures: list[str] = []
    if sqlite_integrity.lower() != "ok":
        failures.append(f"sqlite_integrity:{sqlite_integrity}")
    if foreign_key_violations:
        failures.append(f"foreign_key_violations:{foreign_key_violations}")
    if not source.database_evidence_ok:
        failures.append("source_database_evidence_failed")
    if not backup.database_evidence_ok:
        failures.append("backup_database_evidence_failed")
    if not source.processing_order_complete:
        failures.append("source_processing_order_incomplete")
    if not backup.processing_order_complete:
        failures.append("backup_processing_order_incomplete")
    if source.signal_count != backup.signal_count:
        failures.append(f"signal_count:{source.signal_count}!={backup.signal_count}")
    if source.state_fingerprint != backup.state_fingerprint:
        failures.append("state_fingerprint_mismatch")
    if source.integrity_head != backup.integrity_head:
        failures.append("integrity_head_mismatch")
    if source.policy_fingerprint != backup.policy_fingerprint:
        failures.append("policy_fingerprint_mismatch")
    if source.process_order_fingerprint != backup.process_order_fingerprint:
        failures.append("process_order_fingerprint_mismatch")
    return BackupVerification(
        source=source,
        backup=backup,
        backup_file=fingerprint_file(backup_path),
        sqlite_integrity=sqlite_integrity,
        foreign_key_violations=foreign_key_violations,
        verified=not failures,
        failures=tuple(failures),
    )


def create_verified_backup(
    source_path: str | Path,
    backup_path: str | Path,
    *,
    overwrite: bool = False,
    runtime_policy: RuntimePolicyBundle | None = None,
) -> BackupVerification:
    source = Path(source_path)
    backup = Path(backup_path)
    if not source.exists():
        raise FileNotFoundError(source)
    if backup.exists() and not overwrite:
        raise FileExistsError(backup)
    if backup.resolve() == source.resolve():
        raise ValueError("backup path must differ from source path")
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        backup.unlink()

    with (
        sqlite3.connect(str(source), uri=False) as source_db,
        sqlite3.connect(str(backup)) as backup_db,
    ):
        source_db.backup(backup_db)
        backup_db.commit()

    verification = verify_backup(source, backup, runtime_policy=runtime_policy)
    if not verification.verified:
        raise RuntimeError("backup verification failed: " + ",".join(verification.failures))
    return verification

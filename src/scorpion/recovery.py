from __future__ import annotations

import hashlib
import json
import os
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
    """Observed-book replay and evidence snapshot; not a snapshot of confirmed fills."""

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
        database_evidence_ok=evidence.ok and evidence.legacy_uncovered_signals == 0,
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
    source_path_obj = Path(source_path)
    backup_path_obj = Path(backup_path)
    if not source_path_obj.is_file():
        raise FileNotFoundError(source_path_obj)
    if not backup_path_obj.is_file():
        raise FileNotFoundError(backup_path_obj)

    policy = runtime_policy or RuntimePolicyBundle()
    source = _snapshot(source_path_obj, runtime_policy=policy)
    backup = _snapshot(backup_path_obj, runtime_policy=policy)
    sqlite_integrity, foreign_key_violations = _database_checks(backup_path_obj)
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
        backup_file=fingerprint_file(backup_path_obj),
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
    if not source.is_file():
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


def recover_paper_session_journal(
    source: Path, destination: Path, *, max_bytes: int = 64 * 1024 * 1024,
) -> dict[str, object]:
    """Preserve an interrupted paper session without replaying a single effect.

    Only the last fsync-bound prefix can be recovered as evidence. The original
    and every unverified trailing byte remain available for operator forensics.
    This is not automatic restart authorization and never clears a safety latch.
    """
    from .session_service import (
        JournalError,
        _fsync_directory,
        journal_checkpoint_path,
        journal_identity_bytes,
        journal_receipt_path,
    )

    if max_bytes <= 0:
        raise ValueError("journal recovery bound must be positive")
    sealed = journal_receipt_path(source).exists()
    checkpoint = journal_receipt_path(source) if sealed else journal_checkpoint_path(source)
    try:
        metadata_bytes = checkpoint.read_bytes()
        meta = json.loads(metadata_bytes)
        with source.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if checkpoint.read_bytes() != metadata_bytes:
            raise JournalError("journal_checkpoint_changed_during_recovery")
    except (OSError, ValueError) as exc:
        raise JournalError("journal_recovery_checkpoint_unavailable") from exc
    if len(raw) > max_bytes:
        raise JournalError("journal_recovery_bound_exceeded")
    if not isinstance(meta, dict) or type(meta.get("bytes")) is not int:
        raise JournalError("journal_recovery_checkpoint_invalid")
    size = meta["bytes"]
    if not 0 <= size <= len(raw):
        raise JournalError("journal_recovery_checkpoint_outside_file")
    prefix, tail = raw[:size], raw[size:]
    if meta != journal_identity_bytes(prefix):
        raise JournalError("journal_recovery_prefix_mismatch")
    if sealed and tail:
        raise JournalError("sealed_journal_has_trailing_bytes")
    # Never create a replacement journal at the original path, and never write a
    # clean-session receipt for a recovered prefix.
    destination.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {
        "schema": "scorpion.paper-journal-recovery.v1",
        "authority": "RESEARCH_ONLY",
        "status": "SEALED_COPY" if sealed else "VERIFIED_PREFIX_QUARANTINED",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "checkpoint_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
        "verified_prefix": meta,
        "unverified_tail_bytes": len(tail),
        "unverified_tail_sha256": hashlib.sha256(tail).hexdigest(),
        "resume_authorized": False,
        "certifies_fills": False,
    }
    for name, content in (
        ("verified-prefix.jsonl", prefix), ("unverified-tail.bin", tail),
        ("source-checkpoint.json", metadata_bytes),
    ):
        with (destination / name).open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    with (destination / "recovery.json").open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(destination)
    _fsync_directory(destination.parent)
    return manifest

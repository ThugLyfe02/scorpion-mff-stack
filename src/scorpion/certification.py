from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .integrity import IntegrityLedger
from .policy_bundle import RuntimePolicyBundle
from .recovery import BackupVerification, create_verified_backup
from .replay import replay, state_fingerprint
from .schema_contract import inspect_schema
from .state_checkpoint import restore_state
from .store import Store
from .temporal_guard import TemporalStreamReport, load_temporal_stream_report


@dataclass(frozen=True, slots=True)
class CertificationCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RuntimeCertificationReport:
    checks: tuple[CertificationCheck, ...]
    state_fingerprint: str
    temporal: TemporalStreamReport
    backup: BackupVerification | None
    passed: bool


def certify_runtime(
    path: str | Path,
    *,
    backup_path: str | Path | None = None,
    overwrite_backup: bool = False,
) -> RuntimeCertificationReport:
    policy = RuntimePolicyBundle()
    store = Store(path)
    signals = store.load_signals()
    state, _ = replay(signals, policy.base)
    fingerprint = state_fingerprint(state)
    duplicate_state, _ = replay(tuple(signals) + tuple(signals), policy.base)
    reversed_state, _ = replay(tuple(reversed(signals)), policy.base)
    restored = restore_state(path, signals, runtime_policy=policy)
    restored_fingerprint = state_fingerprint(restored.state)
    schema = inspect_schema(path)
    integrity = IntegrityLedger(path).verify_database()
    temporal = load_temporal_stream_report(path)
    checks: list[CertificationCheck] = [
        CertificationCheck(
            "schema_contract",
            schema.compatible,
            "compatible" if schema.compatible else ",".join(schema.failures),
        ),
        CertificationCheck(
            "database_evidence_integrity",
            integrity.ok,
            "verified" if integrity.ok else ",".join(integrity.failures),
        ),
        CertificationCheck(
            "duplicate_event_replay_invariance",
            state_fingerprint(duplicate_state) == fingerprint,
            "duplicate normalized events do not change deterministic state",
        ),
        CertificationCheck(
            "input_order_replay_invariance",
            state_fingerprint(reversed_state) == fingerprint,
            "replay sorting makes input enumeration order irrelevant",
        ),
        CertificationCheck(
            "checkpoint_replay_equivalence",
            restored_fingerprint == fingerprint,
            (
                f"policy={policy.fingerprint};checkpoint={restored.checkpoint_id};"
                f"tail_events={restored.tail_events};mode={restored.reason}"
            ),
        ),
        CertificationCheck(
            "temporal_integrity",
            not temporal.critical,
            (
                "causal clocks clean"
                if not temporal.critical
                else (
                    f"future={temporal.future_source_events},"
                    f"edit_errors={temporal.edit_order_errors},"
                    f"regressions={temporal.source_regressions}"
                )
            ),
        ),
    ]
    backup: BackupVerification | None = None
    if backup_path is not None:
        backup = create_verified_backup(
            path,
            backup_path,
            overwrite=overwrite_backup,
        )
        backup_detail = (
            "backup state/evidence matches source"
            if backup.verified
            else ",".join(backup.failures)
        )
        checks.append(
            CertificationCheck(
                "verified_backup_replay_equivalence",
                backup.verified,
                backup_detail,
            )
        )
    return RuntimeCertificationReport(
        checks=tuple(checks),
        state_fingerprint=fingerprint,
        temporal=temporal,
        backup=backup,
        passed=all(item.passed for item in checks),
    )

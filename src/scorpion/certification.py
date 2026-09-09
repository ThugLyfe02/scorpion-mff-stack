from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .causal_features import verify_feature_store
from .failure_quarantine import quarantined_count
from .integrity import IntegrityLedger
from .policy_bundle import RuntimePolicyBundle
from .processing_order import inspect_processing_order, load_signals_in_processing_order
from .recovery import BackupVerification, create_verified_backup
from .replay import ReplayOrder, replay, state_fingerprint
from .schema_contract import inspect_schema
from .state_checkpoint import restore_state
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
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)

    policy = RuntimePolicyBundle()
    signals = load_signals_in_processing_order(source)
    state, _ = replay(signals, policy.base, order=ReplayOrder.INPUT)
    fingerprint = state_fingerprint(state)
    duplicate_state, _ = replay(
        tuple(signals) + tuple(signals),
        policy.base,
        order=ReplayOrder.INPUT,
    )
    restored = restore_state(source, signals, runtime_policy=policy)
    restored_fingerprint = state_fingerprint(restored.state)
    schema = inspect_schema(source)
    integrity = IntegrityLedger(source).verify_database()
    integrity_failures = list(integrity.failures)
    if integrity.legacy_uncovered_signals:
        integrity_failures.append(
            f"signals_outside_integrity_ledger:{integrity.legacy_uncovered_signals}"
        )
    integrity_strict = integrity.ok and integrity.legacy_uncovered_signals == 0
    temporal = load_temporal_stream_report(source)
    processing_order = inspect_processing_order(source)
    feature_store = verify_feature_store(source)
    quarantined = quarantined_count(source)
    checks: list[CertificationCheck] = [
        CertificationCheck(
            "schema_contract",
            schema.compatible,
            "compatible" if schema.compatible else ",".join(schema.failures),
        ),
        CertificationCheck(
            "database_evidence_integrity",
            integrity_strict,
            (
                f"verified:{integrity.checked}"
                if integrity_strict
                else ",".join(integrity_failures) or "database_evidence_failed"
            ),
        ),
        CertificationCheck(
            "durable_processing_order",
            processing_order.complete,
            (
                f"signals={processing_order.signal_count};"
                f"ordered={processing_order.process_ordered_count};"
                f"fingerprint={processing_order.process_fingerprint}"
            ),
        ),
        CertificationCheck(
            "duplicate_event_replay_invariance",
            state_fingerprint(duplicate_state) == fingerprint,
            "duplicate normalized events do not change durable-process-order state",
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
            "causal_feature_store_integrity",
            feature_store.valid,
            (
                f"snapshots={feature_store.snapshots};labeled={feature_store.labeled_rows};"
                f"hash_mismatches={feature_store.hash_mismatches};"
                f"invalid_seq={feature_store.invalid_process_sequences}"
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
        CertificationCheck(
            "poison_event_quarantine_visible",
            True,
            f"quarantined_raw_revisions={quarantined}",
        ),
    ]
    backup: BackupVerification | None = None
    if backup_path is not None:
        backup = create_verified_backup(
            source,
            backup_path,
            overwrite=overwrite_backup,
            runtime_policy=policy,
        )
        backup_detail = (
            "backup state/evidence/order matches source"
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

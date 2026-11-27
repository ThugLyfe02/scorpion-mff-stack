"""Crash forensic audit anchored to an independently retained paper ledger checkpoint.

Only reconstructs and classifies observations. No writes, order actions, latch
clears, restoration, or authorization for automatic resumption.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .delivery import PaperAccountSnapshot, PaperDeliveryLedger, PaperRiskPolicy
from .paper_evidence_collector import replay_venue_artifact
from .provenance import stable_hash


@dataclass(frozen=True, slots=True)
class RecoveryOrder:
    client_id: str
    call_id: str | None
    role: str | None
    local_state: str | None
    external_state: str | None
    local_filled: int | None
    external_filled: int | None
    status: str
    requires_operator_review: bool


@dataclass(frozen=True, slots=True)
class PaperRecoveryAudit:
    policy_sha256: str
    session_date: date
    anchor_sequence: int
    anchor_head: str
    candidate_sequence: int
    candidate_head: str
    unanchored_events: int
    source_artifact_sha256: str | None
    source_cutoff: datetime | None
    order_checks: tuple[RecoveryOrder, ...]
    expected_positions: tuple[tuple[str, int], ...]
    pending_sell_quantities: tuple[tuple[str, int], ...]
    buy_commitment_usd: Decimal
    cash_residual_usd: Decimal | None
    blockers: tuple[str, ...]
    report_sha256: str
    authority: str = "RECOVERY_REVIEW_ONLY"
    anchor_custody_verified: bool = False
    certifies_external_account: bool = False
    resume_authorized: bool = False
    authorizes_orders: bool = False


def audit_paper_recovery(
    *, ledger_path: Path, policy: PaperRiskPolicy, anchor_sequence: int, anchor_head: str,
    source: Path | None = None, source_pin: str | None = None,
) -> PaperRecoveryAudit:
    """Check the latest external anchor and optionally replay a captured venue archive.

    Missing venue identities remain unknown, even with a complete post-close
    archive: absence never proves an uncertain side effect did not occur.
    """
    if (source is None) != (source_pin is None):
        raise ValueError("recovery source requires path and pin together")
    snapshot = None
    source_cutoff = None
    blockers: set[str] = set()
    if source is not None and source_pin is not None:
        receipt, view = replay_venue_artifact(
            source, source_pin, account=policy.account_id, day=policy.session_date,
        )
        source_cutoff = receipt.started_at
        snapshot = PaperAccountSnapshot(
            policy.account_id, source_cutoff, view.cash, view.buying_power, view.orders,
            view.positions, receipt.source_artifact_sha256, view.blocked,
        )
        blockers.update(receipt.blockers)
    else:
        blockers.add("external_observation_required")
    report = PaperDeliveryLedger(ledger_path, policy).inspect_lineage(
        anchor_sequence=anchor_sequence, anchor_head=anchor_head, snapshot=snapshot,
    )
    blockers.update(report.blockers)
    unanchored = report.events-anchor_sequence
    if unanchored:
        blockers.add("unanchored_suffix_requires_independent_review")
    if report.expected_positions:
        blockers.add("local_inventory_not_flat")
    if report.pending_sell_quantities:
        blockers.add("pending_sell_reservations")
    external = {order.client_id: order for order in snapshot.orders} if snapshot else {}
    local = {order.client_id: order for order in report.orders}
    rows: list[RecoveryOrder] = []
    for key in sorted(local.keys() | external.keys()):
        record, observation = local.get(key), external.get(key)
        if record is None:
            status = "UNREGISTERED_EXTERNAL_ORDER"
        elif snapshot is None:
            status = "VENUE_EVIDENCE_NOT_COLLECTED"
        elif observation is None:
            status = "ABSENT_EXTERNAL_ORDER_NOT_PROOF_OF_NONEXECUTION"
        elif record.observation is None:
            status = "EXTERNAL_ORDER_WITH_UNACKNOWLEDGED_LOCAL_DISPATCH"
        elif record.observation != observation:
            status = "LOCAL_AND_EXTERNAL_STATE_DIFFER"
        elif not observation.terminal:
            status = "MATCHED_BUT_NONTERMINAL"
        else:
            status = "MATCHED_TERMINAL_OBSERVATION"
        if record is not None and source_cutoff is not None and record.dispatched_at > source_cutoff:
            status = "SOURCE_PRECEDES_LOCAL_DISPATCH"
        review = status != "MATCHED_TERMINAL_OBSERVATION"
        if review:
            blockers.add("order_identity_or_state_requires_review")
        rows.append(RecoveryOrder(
            key, record.request.call_id if record else None,
            record.request.role if record else None, record.state if record else None,
            observation.state if observation else None, record.filled if record else None,
            observation.filled if observation else None, status, review,
        ))
    fields = (
        policy.digest, policy.session_date, anchor_sequence, anchor_head, report.events,
        report.chain_sha256, unanchored, source_pin, source_cutoff, tuple(rows),
        report.expected_positions, report.pending_sell_quantities,
        report.session_buy_commitment_usd, report.cash_residual_usd, tuple(sorted(blockers)),
    )
    return PaperRecoveryAudit(*fields, stable_hash(("paper-recovery-audit-v1", fields)))

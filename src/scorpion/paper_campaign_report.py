"""Read-only operational campaign report; never certifies fills or authorizes action."""
from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .delivery import PaperDeliveryLedger
from .paper_venue import load_paper_risk_policy
from .paper_worker import load_worker_authorization
from .paper_worker_calibration import _coverage
from .provenance import stable_hash
from .session_service import read_verified_paper_records

_MAX_NS = 86_400 * 1_000_000_000
_LATENCY_REASONS = frozenset({
    "durable_ack_deadline", "supervision_or_candidate_deadline",
    "durable intent too old for dispatch", "entry quote stale at dispatch",
    "paper route window expired", "paper dispatch validity expired",
    "working_order_deadline_priority",
})


@dataclass(frozen=True, slots=True)
class TimingDistribution:
    phase: str
    samples: int
    minimum_ms: float | None
    median_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    maximum_ms: float | None
    estimand: str = "EMPIRICAL_OBSERVED_MONOTONIC_DURATIONS_NOT_LATENCY_SLO"


def _duration(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _MAX_NS:
        raise ValueError("campaign invalid duration")
    return value


def timing_distribution(phase: str, values: list[int]) -> TimingDistribution:
    ordered = sorted(_duration(value) for value in values)

    def rank(q: float, minimum: int) -> float | None:
        if len(ordered) < minimum:
            return None
        return ordered[max(0, math.ceil(q*len(ordered))-1)]/1e6

    return TimingDistribution(
        phase, len(ordered), rank(0, 1),
        statistics.median(ordered)/1e6 if ordered else None, rank(0.95, 20),
        rank(0.99, 100), rank(1, 1),
    )


@dataclass(frozen=True, slots=True)
class CampaignOperationsReport:
    journal_sha256: str
    audit_sha256: str
    ledger_head: str
    session_date: str
    candidates: int
    dispatch_reservations: int
    not_dispatched: int
    deadline_or_supervision_refusals: int
    dispositions: tuple[tuple[str, int], ...]
    order_states: tuple[tuple[str, int], ...]
    timings: tuple[TimingDistribution, ...]
    minimum_reservation_slack_ms: float | None
    blockers: tuple[str, ...]
    report_sha256: str
    authority: str = "RESEARCH_ONLY"
    certifies_external_account: bool = False
    certifies_execution_latency: bool = False
    authorizes_training: bool = False
    authorizes_orders: bool = False


def build_campaign_report(
    *, journal: Path, audit_path: Path, worker_config: Path, worker_pin: str,
    risk_path: Path, ledger_path: Path, ledger_head: str,
) -> CampaignOperationsReport:
    """Preserve candidate denominator; distinguish durable reservation from network send.

    This is local telemetry, not a substitute for independent venue observation.
    Missing timing samples are blockers, never fabricated zeros.
    """
    auth = load_worker_authorization(worker_config, worker_pin)
    policy = load_paper_risk_policy(risk_path, auth.risk_pin)
    jr, records = read_verified_paper_records(journal)
    ar, audit = read_verified_paper_records(audit_path)
    if (
        not records or records[0].get("kind") != "session_start"
        or records[0].get("session_date") != policy.session_date.isoformat()
        or not audit or audit[0].get("kind") != "worker_start"
        or audit[0].get("authorization_sha256") != auth.digest
        or audit[0].get("policy_sha256") != policy.digest
        or audit[0].get("ledger_head") != auth.opening_head
        or any(row.get("authority") != "RESEARCH_ONLY" for row in audit)
    ):
        raise ValueError("campaign report identity mismatch")
    candidates, dispositions = _coverage(records, audit)
    ledger = PaperDeliveryLedger(ledger_path, policy).inspect_status(expected_head=ledger_head)
    reserved = {seq for seq, row in dispositions.items()
                if row["disposition"] == "DURABLE_DISPATCH"}
    if (
        len(ledger.orders) != len(reserved)
        or {order.request.intent_seq for order in ledger.orders} != reserved
        or any(order.request.call_id != candidates[order.request.intent_seq].get("call_id")
               or order.request.role != dispositions[order.request.intent_seq].get("role")
               for order in ledger.orders)
    ):
        raise ValueError("campaign report dispatch coverage mismatch")
    blockers: set[str] = set()
    if records[-1].get("kind") != "session_end":
        blockers.add("source_session_incomplete")
    if (
        audit[-1].get("kind") != "worker_shutdown_final"
        or audit[-1].get("ledger_head") != ledger_head
        or audit[-1].get("blockers") != []
        or any(row.get("kind") == "worker_fault" for row in audit)
    ):
        blockers.add("worker_session_incomplete")
    if ledger.expected_positions or any(
        order.observation is None or not order.observation.terminal for order in ledger.orders
    ):
        blockers.add("local_order_or_inventory_unresolved")
    reasons: Counter[str] = Counter()
    misses = 0
    offer: list[int] = []
    for row in dispositions.values():
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ValueError("campaign disposition reason missing")
        reasons[reason] += 1
        if row["disposition"] == "NOT_DISPATCHED" and reason in _LATENCY_REASONS:
            misses += 1
        if "elapsed_from_offer_ns" not in row:
            blockers.add("missing_disposition_timing")
        else:
            offer.append(_duration(row["elapsed_from_offer_ns"]))
    phases: dict[str, list[int]] = {
        "dispatch": [], "reconcile": [], "offer_to_durable_observed": [],
    }
    seen: dict[str, set[int]] = {"dispatch": set(), "offer_to_durable_observed": set()}
    for row in audit:
        if row.get("kind") != "worker_latency":
            continue
        phase = row.get("phase")
        if not isinstance(phase, str) or phase not in phases:
            raise ValueError("campaign unknown timing phase")
        if phase in seen:
            seq = row.get("intent_seq")
            if (
                not isinstance(seq, int) or isinstance(seq, bool)
                or seq not in candidates or seq in seen[phase]
                or (phase == "dispatch" and seq not in reserved)
            ):
                raise ValueError("campaign timing identity mismatch")
            seen[phase].add(seq)
        phases[phase].append(_duration(row.get("elapsed_ns")))
    if seen["dispatch"] != reserved or not reserved <= seen["offer_to_durable_observed"]:
        blockers.add("missing_dispatch_timing")
    slack: list[float] = []
    for order in ledger.orders:
        req = order.request
        until = min(req.deadline, req.admit_until or req.deadline)
        remaining = (until-order.dispatched_at).total_seconds()*1000
        if remaining < 0:
            blockers.add("reservation_after_validity")
        slack.append(remaining)
    if not reserved:
        blockers.add("no_dispatch_observations")
    timings = tuple(timing_distribution(name, values) for name, values in sorted({
        **phases, "offer_to_disposition": offer,
    }.items()))
    fields = (
        str(jr["sha256"]), str(ar["sha256"]), ledger_head, policy.session_date.isoformat(),
        len(candidates), len(reserved), len(candidates)-len(reserved), misses,
        tuple(sorted(reasons.items())),
        tuple(sorted(Counter(o.state for o in ledger.orders).items())),
        timings, min(slack) if slack else None, tuple(sorted(blockers)),
    )
    return CampaignOperationsReport(*fields, stable_hash(("paper-campaign-ops-v1", fields)))

"""Read-only paper campaign preflight; explicitly authorized GET requests only."""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import MARKET_TZ
from .delivery import PaperDeliveryLedger
from .paper_venue import AlpacaEquityPaperVenue, _latch_ok, load_paper_risk_policy
from .paper_worker import load_worker_authorization
from .provenance import stable_hash
from .session_calendar import session_hours


@dataclass(frozen=True, slots=True)
class CampaignProbe:
    authorization_sha256: str
    policy_sha256: str
    ledger_head: str
    measured_at: datetime
    snapshot_ms: tuple[float, ...]
    clock_ms: tuple[float, ...]
    max_combined_ms: float | None
    entry_budget_ms: int
    blockers: tuple[str, ...]
    report_sha256: str
    phase: str = "REGULAR_SESSION"
    authority: str = "PAPER_READ_ONLY"
    authorizes_orders: bool = False
    certifies_execution_latency: bool = False


def probe_campaign(
    *, worker_config: Path, worker_pin: str, risk_path: Path,
    ledger_path: Path, latch_path: Path, samples: int = 3,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    pre_open: bool = False,
) -> CampaignProbe:
    if type(samples) is not int or not 1 <= samples <= 5:
        raise ValueError("probe sample bound")
    authorization = load_worker_authorization(worker_config, worker_pin)
    policy = load_paper_risk_policy(risk_path, authorization.risk_pin)
    ledger = PaperDeliveryLedger(ledger_path, policy)
    status = ledger.status(expected_head=authorization.opening_head)
    if status.events != 1 or status.orders:
        raise ValueError("probe requires initialized opening ledger")
    if set(dict(authorization.exit_limits)) != set(dict(policy.symbols_and_groups)):
        raise ValueError("probe exit roster mismatch")
    now = clock()
    hours = session_hours(policy.session_date)
    if (
        hours is None or now.tzinfo is None or now.utcoffset() is None
        or now.astimezone(MARKET_TZ).date() != policy.session_date
        or (pre_open and not now < hours[0])
        or (not pre_open and not hours[0] <= now < hours[1])
    ):
        raise ValueError("probe outside pinned session phase")
    if not _latch_ok(latch_path):
        raise ValueError("probe safety latch not armed")
    venue = AlpacaEquityPaperVenue(policy.account_id, clock=clock)
    snapshots: list[float] = []
    clocks: list[float] = []
    blockers: set[str] = set()
    for _ in range(samples):
        started = monotonic_ns()
        snapshot = venue.snapshot(policy.session_date)
        snapshots.append((monotonic_ns() - started) / 1e6)
        started = monotonic_ns()
        opened = venue.market_clock(clock()) if pre_open else venue.market_open(clock())
        clocks.append((monotonic_ns() - started) / 1e6)
        if snapshot.orders or snapshot.positions or snapshot.trading_blocked:
            blockers.add("account_not_exclusive_flat")
        if pre_open and opened:
            blockers.add("venue_open_before_pinned_open")
        if not pre_open and not opened:
            blockers.add("venue_market_closed")
        stamp = clock()
        if (pre_open and stamp >= hours[0]) or (not pre_open and stamp >= hours[1]):
            blockers.add("probe_crossed_session_boundary")
            break
        if snapshot.cash <= policy.fee_cash_buffer_usd:
            blockers.add("insufficient_cash")
        opening = ledger.inspect_snapshot(snapshot, expected_head=status.chain_sha256)
        if opening.cash_residual_usd != 0:
            blockers.add("opening_cash_changed_after_pin")
        if not _latch_ok(latch_path):
            blockers.add("latch_changed")
            break
    combined = max((s + c for s, c in zip(snapshots, clocks, strict=True)), default=None)
    # These GET timings are a lower bound on the complete order path. Reserve
    # half the current quote TTL for journal, validation, reservation and send.
    if combined is None or not math.isfinite(combined) or combined > 1500:
        blockers.add("read_latency_exceeds_probe_budget")
    if len(snapshots) != samples:
        blockers.add("probe_incomplete")
    # A concurrent operator modifying the ledger must not pass this probe.
    ledger.status(expected_head=status.chain_sha256)
    fields = (
        authorization.digest, policy.digest, status.chain_sha256, now,
        tuple(snapshots), tuple(clocks), combined, 3000, tuple(sorted(blockers)),
    )
    phase = "PRE_OPEN" if pre_open else "REGULAR_SESSION"
    return CampaignProbe(
        *fields, stable_hash(("paper-campaign-probe-v2", fields, phase)), phase=phase,
    )

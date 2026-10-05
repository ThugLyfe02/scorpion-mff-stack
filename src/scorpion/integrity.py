from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .domain import BookState, RawDiscordMessage
from .invariants import assert_valid_book
from .replay import replay, state_fingerprint
from .store import Store


@dataclass(frozen=True, slots=True)
class DurableIntegrityReport:
    ok: bool
    quick_check: str
    foreign_key_violations: int
    invalid_raw_statuses: int
    raw_revision_mismatches: int
    orphan_effects: int
    orphan_audits: int
    signals_missing_audit: int
    audit_kind_mismatches: int

    @property
    def reason_codes(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.quick_check != "ok":
            reasons.append("sqlite_quick_check")
        if self.foreign_key_violations:
            reasons.append("foreign_key_violation")
        if self.invalid_raw_statuses:
            reasons.append("invalid_raw_status")
        if self.raw_revision_mismatches:
            reasons.append("raw_revision_mismatch")
        if self.orphan_effects:
            reasons.append("orphan_effect")
        if self.orphan_audits:
            reasons.append("orphan_decision_audit")
        if self.signals_missing_audit:
            reasons.append("signal_missing_decision_audit")
        if self.audit_kind_mismatches:
            reasons.append("audit_kind_mismatch")
        return tuple(reasons)


def audit_durable_integrity(store: Store) -> DurableIntegrityReport:
    """Check structural invariants that replay equivalence alone cannot prove.

    This intentionally runs on the sentinel cadence rather than the message hot path.
    It detects database corruption plus partial/manual mutations that can leave the
    event log apparently replayable while its audit/effect bundle is inconsistent.
    """
    with store.connect() as db:
        quick_rows = db.execute("PRAGMA quick_check").fetchall()
        quick_check = "ok" if len(quick_rows) == 1 and quick_rows[0][0] == "ok" else "failed"
        foreign_key_violations = len(db.execute("PRAGMA foreign_key_check").fetchall())
        invalid_raw_statuses = db.execute(
            "SELECT COUNT(*) FROM raw_processing WHERE status NOT IN ('PENDING','DONE')"
        ).fetchone()[0]
        raw_rows = db.execute("SELECT * FROM raw_discord_events").fetchall()
        raw_revision_mismatches = 0
        for row in raw_rows:
            raw = RawDiscordMessage(
                message_id=row["message_id"],
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                author_id=row["author_id"],
                content=row["content"],
                source_ts_utc=datetime.fromisoformat(row["source_ts_utc"]),
                received_ts_utc=datetime.fromisoformat(row["received_ts_utc"]),
                edited_ts_utc=(
                    datetime.fromisoformat(row["edited_ts_utc"])
                    if row["edited_ts_utc"]
                    else None
                ),
                referenced_message_id=row["referenced_message_id"],
            )
            if raw.revision_id != row["raw_event_id"] or raw.content_sha256 != row["content_sha256"]:
                raw_revision_mismatches += 1
        orphan_effects = db.execute(
            """
            SELECT COUNT(*) FROM proposed_effects e
            LEFT JOIN signal_events s ON s.event_id=e.source_event_id
            WHERE s.event_id IS NULL
            """
        ).fetchone()[0]
        orphan_audits = db.execute(
            """
            SELECT COUNT(*) FROM decision_audit a
            LEFT JOIN signal_events s ON s.event_id=a.event_id
            WHERE s.event_id IS NULL
            """
        ).fetchone()[0]
        signals_missing_audit = db.execute(
            """
            SELECT COUNT(*) FROM signal_events s
            LEFT JOIN decision_audit a ON a.event_id=s.event_id
            WHERE a.event_id IS NULL
            """
        ).fetchone()[0]
        audit_kind_mismatches = db.execute(
            """
            SELECT COUNT(*) FROM signal_events s
            JOIN decision_audit a ON a.event_id=s.event_id
            WHERE s.kind != a.kind
            """
        ).fetchone()[0]

    counts = (
        foreign_key_violations,
        invalid_raw_statuses,
        raw_revision_mismatches,
        orphan_effects,
        orphan_audits,
        signals_missing_audit,
        audit_kind_mismatches,
    )
    return DurableIntegrityReport(
        ok=quick_check == "ok" and not any(counts),
        quick_check=quick_check,
        foreign_key_violations=foreign_key_violations,
        invalid_raw_statuses=invalid_raw_statuses,
        raw_revision_mismatches=raw_revision_mismatches,
        orphan_effects=orphan_effects,
        orphan_audits=orphan_audits,
        signals_missing_audit=signals_missing_audit,
        audit_kind_mismatches=audit_kind_mismatches,
    )


@dataclass(frozen=True, slots=True)
class IntegrityCheckResult:
    ok: bool
    live_fingerprint: str
    durable_fingerprint: str
    durable_event_count: int
    durable_integrity: DurableIntegrityReport


@dataclass(slots=True)
class ReplayIntegritySentinel:
    """Periodically prove live/replay identity and durable bundle integrity.

    The check is intentionally off the per-message hot path. Any structural or
    replay mismatch durably revokes admission before diagnostic telemetry is emitted.
    """

    every_n_commits: int = 64
    _successful_commits: int = 0

    def __post_init__(self) -> None:
        if self.every_n_commits <= 0:
            raise ValueError("every_n_commits must be positive")

    def after_commit(
        self,
        store: Store,
        live_state: BookState,
        *,
        force: bool = False,
    ) -> IntegrityCheckResult | None:
        self._successful_commits += 1
        if not force and self._successful_commits % self.every_n_commits:
            return None

        durable_integrity = audit_durable_integrity(store)
        events = store.load_signals()
        durable_state, _ = replay(events)
        assert_valid_book(durable_state)
        live_fingerprint = state_fingerprint(live_state)
        durable_fingerprint = state_fingerprint(durable_state)
        replay_ok = live_fingerprint == durable_fingerprint
        ok = replay_ok and durable_integrity.ok

        # Revoke admission before telemetry. Once a bad state is observed, no
        # diagnostic failure may allow subsequent normalized progression.
        if not ok:
            reason = (
                "durable_integrity_violation"
                if not durable_integrity.ok
                else "replay_integrity_divergence"
            )
            store.set_halt(True, reason)

        store.heartbeat(
            "replay-integrity",
            status="ok" if ok else "diverged",
            live_fingerprint=live_fingerprint,
            durable_fingerprint=durable_fingerprint,
            durable_event_count=len(events),
            cadence_commits=self.every_n_commits,
            durable_integrity_ok=durable_integrity.ok,
            durable_integrity_reasons=durable_integrity.reason_codes,
        )
        return IntegrityCheckResult(
            ok=ok,
            live_fingerprint=live_fingerprint,
            durable_fingerprint=durable_fingerprint,
            durable_event_count=len(events),
            durable_integrity=durable_integrity,
        )



def clear_integrity_halt_if_safe(store: Store) -> DurableIntegrityReport:
    """Clear a safety latch only after the durable substrate re-passes integrity checks.

    A restart reconstructs live state from the durable log, so durable structural integrity
    is the authoritative prerequisite for releasing an integrity-originated halt.
    """
    report = audit_durable_integrity(store)
    if not report.ok:
        raise RuntimeError(
            "cannot clear integrity halt while durable invariants fail: "
            + ",".join(report.reason_codes)
        )
    store.set_halt(False, "integrity_verified", verified=True)
    store.heartbeat(
        "replay-integrity",
        status="recovered",
        durable_integrity_ok=True,
        durable_integrity_reasons=(),
    )
    return report

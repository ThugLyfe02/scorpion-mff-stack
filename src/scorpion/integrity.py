from __future__ import annotations

from dataclasses import dataclass

from .domain import BookState
from .invariants import assert_valid_book
from .replay import replay, state_fingerprint
from .store import Store


@dataclass(frozen=True, slots=True)
class IntegrityCheckResult:
    ok: bool
    live_fingerprint: str
    durable_fingerprint: str
    durable_event_count: int


@dataclass(slots=True)
class ReplayIntegritySentinel:
    """Periodically prove that in-memory state equals replay of the durable event log.

    The check is intentionally off the per-message hot path. A mismatch latches the runtime
    halt flag and emits a heartbeat, but does not rewrite the event log or fabricate state.
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

        events = store.load_signals()
        durable_state, _ = replay(events)
        assert_valid_book(durable_state)
        live_fingerprint = state_fingerprint(live_state)
        durable_fingerprint = state_fingerprint(durable_state)
        ok = live_fingerprint == durable_fingerprint

        # Durably revoke admission before any diagnostic write can fail or the
        # process can exit. Telemetry must not be a prerequisite for the latch.
        if not ok:
            store.set_halt(True, "replay_integrity_divergence")

        store.heartbeat(
            "replay-integrity",
            status="ok" if ok else "diverged",
            live_fingerprint=live_fingerprint,
            durable_fingerprint=durable_fingerprint,
            durable_event_count=len(events),
            cadence_commits=self.every_n_commits,
        )
        return IntegrityCheckResult(
            ok=ok,
            live_fingerprint=live_fingerprint,
            durable_fingerprint=durable_fingerprint,
            durable_event_count=len(events),
        )

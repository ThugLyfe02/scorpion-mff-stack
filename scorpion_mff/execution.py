"""Execution safety: the circuit breaker and the spread gate.

Two guards live here because both are *execution-time* refusals, not sizing or
pricing ones, and both were ported from PR#1 (`guard.py` / `outbox.py`) without
importing that tree.

The circuit breaker is the transport-failure governor. A dead venue plus a live
listener is how one bad morning becomes a hundred hammered orders in thirty
seconds, so consecutive transport failures open the breaker for a cooldown, one
half-open probe is allowed through, a success closes it, and a failure re-opens
it. The gate only blocks BUYS in the listener: exits must always stay placeable
(the kill-switch invariant - you must be able to leave a position you are in),
so the breaker is consulted for entries only.

The spread gate is the "is this quote actually tradeable" guard. A wide bid/ask
is a cost you cannot get back, so `spread_ok` refuses when the spread exceeds a
configured ratio. It is OFF by default (`max_spread_ratio=None`) so the locked
rules and the 93-alert corpus backtest reproduce their original decisions byte
for byte; `EngineConfig.max_spread_ratio` (env `MFF_MAX_SPREAD_RATIO`) opts in.

Both are pure: the breaker takes an injectable clock, and the spread gate is
arithmetic on a `Quote`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .types import Quote


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Stops hammering a venue that is failing.

    Closed until `failure_threshold` consecutive transport failures, then open
    for `cooldown_seconds`. When the cooldown elapses the breaker goes half-open
    and permits exactly ONE probe; a success closes it, a failure re-opens it
    for a fresh cooldown. The clock is injectable so tests are deterministic.
    """

    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    clock: Callable[[], float] = time.time
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    opened_until: float | None = None
    _probe_armed: bool = False

    def allow(self, now: float | None = None) -> bool:
        """May a BUY pass right now? Consumes the single half-open probe.

        `allow` is only meaningful for entries; the listener never consults it
        for exits. An explicit `now` beats the injected clock, so the listener
        can drive the breaker off its own deterministic clock.
        """
        moment = self.clock() if now is None else now
        if self.state is BreakerState.OPEN:
            if moment >= (self.opened_until or 0):
                self.state = BreakerState.HALF_OPEN
                self._probe_armed = True
            else:
                return False
        if self.state is BreakerState.HALF_OPEN:
            if self._probe_armed:
                self._probe_armed = False     # the one probe is now in flight
                return True
            return False
        return True                            # CLOSED

    @property
    def is_open(self) -> bool:
        return self.state is BreakerState.OPEN

    def record_success(self) -> None:
        """Any non-transport outcome (ack or business reject) resets the streak.

        A business reject is transport *working* - the venue answered - so it
        breaks a run of consecutive transport failures just like a fill does.
        """
        self.failures = 0
        self.state = BreakerState.CLOSED
        self.opened_until = None
        self._probe_armed = False

    def record_failure(self, now: float | None = None) -> None:
        """A transport failure. Opens on threshold, or re-opens a failed probe."""
        moment = self.clock() if now is None else now
        if self.state is BreakerState.OPEN:
            return                            # already open; cooldown unchanged
        if self.state is BreakerState.HALF_OPEN:
            self._open(moment)                # the probe failed: full cooldown again
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self._open(moment)

    def _open(self, moment: float) -> None:
        self.failures = self.failure_threshold
        self.state = BreakerState.OPEN
        self.opened_until = moment + self.cooldown_seconds
        self._probe_armed = False


def spread_ratio(quote: Quote) -> float | None:
    """(ask - bid) / bid. None when the bid cannot carry a ratio (fail closed)."""
    if quote.bid <= 0:
        return None
    return (quote.ask - quote.bid) / quote.bid


def spread_ok(quote: Quote, max_ratio: float | None) -> bool:
    """True when the spread is inside `max_ratio`; a disabled gate always passes.

    Off by default: `max_ratio=None` means the gate is not armed and every quote
    passes, which is what keeps the locked rules and the corpus backtest
    unchanged. When armed, exactly `max_ratio` is inside the gate (the boundary
    is not refused) and anything wider is refused. An unpriceable bid (<= 0)
    fails closed when the gate is armed.
    """
    if max_ratio is None:
        return True
    ratio = spread_ratio(quote)
    if ratio is None:
        return False
    # A tiny tolerance keeps the exact boundary inside the gate despite binary
    # float arithmetic (0.10 stores as 0.10000000000000009).
    return ratio <= max_ratio + 1e-9

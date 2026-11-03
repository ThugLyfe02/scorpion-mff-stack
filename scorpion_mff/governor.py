"""The governor: policy + live state -> a multiplier the engine may not exceed.

This is the piece that turns a written risk policy into behaviour. It is a pure
function of (policy, state, signal, clock). No I/O, no randomness, no opinions about
the market - which means every decision can be replayed and explained after the fact,
and a disputed trade has an answer in the fill log: `governor:daily_stop:-2.0R`.

The ladder is applied as the MINIMUM of every rule that binds, so rules cannot cancel
each other: a channel multiplier of 1.0 cannot restore size after a daily stop.

Two independent clamps enforce the invariant from `policy.py`:

    factor = min(factor, MAX_MULTIPLIER)      # in decide()
    qty    = min(qty, floor(qty * factor))    # in apply_contracts()

If a policy is hand-edited past validation, the second clamp still refuses to size up.

Sessions are bucketed from a fixed UTC-04:00 offset (EDT), matching `historic.py`:
the engine only needs ordering and coarse time-of-day, and a tz database would be a
dependency for one subtraction. When the desk moves to a market-hours calendar this is
the function to replace.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .policy import MAX_MULTIPLIER, Policy
from .types import Signal

ET_OFFSET_S = -4 * 3600
SESSION_OPEN = "open"           # 09:30-11:00 ET
SESSION_MIDDAY = "midday"       # 11:00-14:00 ET
SESSION_CLOSE = "close"         # 14:00-16:00 ET
SESSION_OFF_HOURS = "off_hours"


@dataclass
class GovernorState:
    """Live risk state. Mutated by `record_result`, persisted by `save`/`load`.

    Persistence matters more than it looks: a process that forgets its drawdown on
    every restart (a crash loop, or a Task Scheduler relaunch) resets the risk limit
    silently, which is the exact failure a max-drawdown rule exists to prevent. `arm`
    loads this file and saves after every settlement; re-arming after a stand-down is
    deleting `live/governor.json` - one auditable act, not a flag in a running process.
    """

    day: str = ""
    realized_today_usd: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    equity_usd: float = 0.0
    peak_equity_usd: float = 0.0
    halted_reason: str = ""

    def roll(self, day: str) -> None:
        """New day: the daily counters reset, the equity path does not."""
        if self.day == day:
            return
        self.day = day
        self.realized_today_usd = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0

    def record_result(self, pnl_usd: float, day: str) -> None:
        self.roll(day)
        self.realized_today_usd += pnl_usd
        self.trades_today += 1
        self.equity_usd += pnl_usd
        self.peak_equity_usd = max(self.peak_equity_usd, self.equity_usd)
        self.consecutive_losses = self.consecutive_losses + 1 if pnl_usd < 0 else 0

    def correct_result(self, delta_usd: float) -> None:
        """Correct the money a venue answer changed, without counting a second trade.

        The engine books an exit at the price in the post, at intent time. The venue's
        fill can be better, worse, partial or absent, so `delta_usd` moves realised P&L
        and the equity path onto what was actually confirmed. It deliberately does not
        touch `trades_today` or `consecutive_losses`: this is the same trade being made
        accurate, not another one. A sign flip on a correction is rare (it needs a fill
        on the other side of the entry), and the next `arm` rebuilds the streak from the
        confirmed book with `sync_engine`, which is the authority.
        """
        self.realized_today_usd += delta_usd
        self.equity_usd += delta_usd
        self.peak_equity_usd = max(self.peak_equity_usd, self.equity_usd)

    @property
    def drawdown_usd(self) -> float:
        return max(0.0, self.peak_equity_usd - self.equity_usd)

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "realized_today_usd": round(self.realized_today_usd, 2),
                "trades_today": self.trades_today,
                "consecutive_losses": self.consecutive_losses,
                "equity_usd": round(self.equity_usd, 2),
                "peak_equity_usd": round(self.peak_equity_usd, 2),
                "halted_reason": self.halted_reason}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GovernorState:
        return cls(day=str(payload.get("day") or ""),
                   realized_today_usd=float(payload.get("realized_today_usd") or 0.0),
                   trades_today=int(payload.get("trades_today") or 0),
                   consecutive_losses=int(payload.get("consecutive_losses") or 0),
                   equity_usd=float(payload.get("equity_usd") or 0.0),
                   peak_equity_usd=float(payload.get("peak_equity_usd") or 0.0),
                   halted_reason=str(payload.get("halted_reason") or ""))

    def save(self, path: str | os.PathLike[str]) -> None:
        """Atomic write: a torn risk file must not parse as "no drawdown"."""
        from pathlib import Path
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)

    @classmethod
    def load(cls, path: str | os.PathLike[str], *, missing_ok: bool = True) -> GovernorState:
        """Read persisted state. A corrupt file is an error, never a silent reset."""
        from pathlib import Path
        p = Path(path)
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except FileNotFoundError:
            if missing_ok:
                return cls()
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read risk state at {p}: {exc}") from None


@dataclass(frozen=True)
class Decision:
    factor: float
    stand_down: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"factor": round(self.factor, 4), "stand_down": self.stand_down,
                "reasons": list(self.reasons)}

    @property
    def reason_text(self) -> str:
        return "|".join(self.reasons) if self.reasons else "policy:full_size"


def session_for(ts: float) -> str:
    """Coarse ET session bucket. 09:30-16:00 is split open / midday / close."""
    et = time.gmtime(ts + ET_OFFSET_S)
    minutes = et.tm_hour * 60 + et.tm_min
    if 9 * 60 + 30 <= minutes < 11 * 60:
        return SESSION_OPEN
    if 11 * 60 <= minutes < 14 * 60:
        return SESSION_MIDDAY
    if 14 * 60 <= minutes < 16 * 60:
        return SESSION_CLOSE
    return SESSION_OFF_HOURS


def clamp_multiplier(factor: float) -> float:
    """Reduce-only, defensively. A policy can never express size above the lock."""
    if factor != factor:              # NaN: refuse rather than propagate
        return 0.0
    return max(0.0, min(float(factor), MAX_MULTIPLIER))


def apply_contracts(qty: int, factor: float) -> int:
    """Reduce a contract count. Never raises it, never rounds up, never negative."""
    safe = clamp_multiplier(factor)
    if qty <= 0:
        return 0
    return max(0, min(qty, int(qty * safe)))


def decide(policy: Policy, state: GovernorState, *, channel: str = "",
           ts: float | None = None) -> Decision:
    """Every rule that binds, as the minimum factor, with all reasons recorded."""
    ts = time.time() if ts is None else ts
    day = time.strftime("%Y-%m-%d", time.gmtime(ts))
    state.roll(day)

    factors: list[float] = [1.0]
    reasons: list[str] = []
    r = policy.risk_unit_usd
    realized_r = state.realized_today_usd / r if r else 0.0

    # 0. A stand-down that already happened is terminal for this state file. Deleting
    #    live/governor.json (a human act) is what re-arms the desk; a limit that clears
    #    itself the moment equity recovers is not a limit.
    if state.halted_reason:
        factors.append(0.0)
        reasons.append(f"{state.halted_reason} (stand-down until re-arm)")

    # 1. Hard daily stop - the one rule that is a stop, not a scale.
    limit_usd = policy.daily_loss_limit_usd_effective()
    if limit_usd is not None and state.realized_today_usd <= -limit_usd:
        factors.append(0.0)
        reasons.append(f"daily_stop:{realized_r:.2f}R/${-state.realized_today_usd:,.0f}")

    # 2. Drawdown ladder, shallow to deep.
    for rung in policy.ladder:
        if rung.applies(realized_r):
            factors.append(rung.factor)
            reasons.append(f"ladder:{rung.at_r:+.1f}R->x{rung.factor:g}")

    # 3. Drawdown from the equity peak (the account-level version of the same idea).
    if policy.max_drawdown_r is not None and r:
        dd_r = state.drawdown_usd / r
        if dd_r >= abs(policy.max_drawdown_r):
            factors.append(0.0)
            breach = f"max_drawdown:{dd_r:.2f}R>={abs(policy.max_drawdown_r):.2f}R"
            if not state.halted_reason:
                state.halted_reason = breach
            reasons.append(breach)

    # 4. Loss streaks. The count is recorded so the log explains the threshold it hit.
    for streak_step in policy.loss_streak:
        if streak_step.applies(state.consecutive_losses):
            factors.append(streak_step.factor)
            reasons.append(f"streak:{streak_step.at}->x{streak_step.factor:g}"
                           f"(n={state.consecutive_losses})")

    # 5. Channel and session tilts. Reduce-only by validation, clamped anyway.
    channel_factor = policy.channel_multipliers.get(channel, 1.0)
    if channel_factor != 1.0:
        factors.append(channel_factor)
        reasons.append(f"channel:{channel}->x{channel_factor:g}")

    session = session_for(ts)
    session_factor = policy.session_multipliers.get(session, 1.0)
    if session_factor != 1.0:
        factors.append(session_factor)
        reasons.append(f"session:{session}->x{session_factor:g}")

    factor = clamp_multiplier(min(factors))
    return Decision(factor=factor, stand_down=factor <= 0.0, reasons=tuple(reasons))


class Governor(Protocol):
    def __call__(self, state: GovernorState, signal: Signal, ts: float) -> Decision: ...


def make_governor(policy: Policy) -> Governor:
    """Bind a policy into the callable the engine holds."""

    def _governor(state: GovernorState, signal: Signal, ts: float) -> Decision:
        return decide(policy, state, channel=signal.channel, ts=ts)

    return _governor

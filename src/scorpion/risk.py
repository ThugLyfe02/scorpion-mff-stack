"""Risk limits as a pure function of realised P&L.

This is the half of a control plane that decides *whether to trade at all*, and it is
deliberately arithmetic with no I/O, no clock, and no market opinion: given a policy and a
risk state it returns a multiplier and the reasons for it, so any decision can be replayed
and explained months later.

The invariant, enforced three times, is that **risk is reduce-only**:

1. `RiskLimits.__post_init__` rejects any factor outside `[0, 1]` - a configuration that
   tries to size *up* fails at load rather than at 09:31.
2. `decide` takes the **minimum** of every rule that binds, so a channel or session tilt
   cannot restore size after a daily stop.
3. `apply_factor` clamps again and floors, so even a hand-built `RiskDecision` cannot raise
   an approved clip or create a fractional contract.

A stand-down is terminal for the state file: once `halted_reason` is set it survives a day
roll and every later decision stands down until a human clears it. A limit that clears
itself when equity recovers is not a limit - and because the state is a value, "clearing" it
means deliberately constructing a fresh `RiskState`, which is a visible act in code review.

Two counters reset at the market-day roll (realised today, loss streak); the equity path
and the peak do not, because drawdown is not a daily phenomenon.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_FLOOR, Decimal

ZERO = Decimal("0")
ONE = Decimal("1")


class RiskConfigError(ValueError):
    """A risk configuration that is not reduce-only, or otherwise unsafe."""


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Every knob is expressed in dollars and in R (one risk unit).

    Defaults are the locked doctrine: 1R = the $1,500 standard clip, a $3,000 daily stop,
    half size at -1R, stand down at -2R, half at two consecutive losses, stop at three.
    """

    risk_unit_usd: Decimal = Decimal("1500")
    daily_stop_usd: Decimal | None = Decimal("3000")
    max_drawdown_usd: Decimal | None = Decimal("9000")
    ladder: tuple[tuple[Decimal, Decimal], ...] = (
        (Decimal("-1500"), Decimal("0.5")),
        (Decimal("-3000"), ZERO),
    )
    loss_streak: tuple[tuple[int, Decimal], ...] = (
        (2, Decimal("0.5")),
        (3, ZERO),
    )
    channel_multipliers: tuple[tuple[str, Decimal], ...] = ()
    max_multiplier: Decimal = ONE

    def __post_init__(self) -> None:
        if self.risk_unit_usd <= 0:
            raise RiskConfigError("risk_unit_usd must be positive")
        if self.daily_stop_usd is not None and self.daily_stop_usd <= 0:
            raise RiskConfigError("daily_stop_usd must be positive when set")
        if self.max_drawdown_usd is not None and self.max_drawdown_usd <= 0:
            raise RiskConfigError("max_drawdown_usd must be positive when set")
        if self.max_multiplier != ONE:
            raise RiskConfigError(
                "max_multiplier must be 1: risk is reduce-only, it can never size up"
            )
        previous = ZERO
        for threshold, factor in self.ladder:
            if threshold >= 0:
                raise RiskConfigError(f"ladder threshold must be negative, got {threshold}")
            if threshold > previous:
                raise RiskConfigError("ladder must be ordered from 0 downwards")
            _check_factor(factor, "ladder")
            previous = threshold
        previous_count = 0
        for count, factor in self.loss_streak:
            if count < 1:
                raise RiskConfigError(f"loss streak threshold must be >= 1, got {count}")
            if count <= previous_count:
                raise RiskConfigError("loss streak thresholds must be strictly increasing")
            _check_factor(factor, "loss_streak")
            previous_count = count
        seen: set[str] = set()
        for channel_id, factor in self.channel_multipliers:
            if channel_id in seen:
                raise RiskConfigError(f"duplicate channel multiplier for {channel_id}")
            seen.add(channel_id)
            _check_factor(factor, "channel_multipliers")

    def channel_factor(self, channel_id: str) -> Decimal:
        for candidate, factor in self.channel_multipliers:
            if candidate == channel_id:
                return factor
        return ONE

    @property
    def daily_stop_r(self) -> Decimal | None:
        if self.daily_stop_usd is None:
            return None
        return self.daily_stop_usd / self.risk_unit_usd


def _check_factor(factor: Decimal, where: str) -> None:
    if not (ZERO <= factor <= ONE):
        raise RiskConfigError(
            f"{where} factor must be within [0, 1] (risk is reduce-only), got {factor}"
        )


DEFAULT_RISK_LIMITS = RiskLimits()


@dataclass(frozen=True, slots=True)
class RiskState:
    """The realised record the limits are evaluated against."""

    market_day: date | None = None
    realized_today: Decimal = ZERO
    equity: Decimal = ZERO
    peak_equity: Decimal = ZERO
    consecutive_losses: int = 0
    halted_reason: str = ""

    @property
    def drawdown(self) -> Decimal:
        return max(ZERO, self.peak_equity - self.equity)

    def rolled(self, market_day: date) -> RiskState:
        """New market day: counters reset, the equity path does not.

        The roll is monotone. A backdated event - a redelivered Discord message, a clock
        step, a replayed row - must never rewind the market day, because rewinding it
        resets `realized_today` and `consecutive_losses` and therefore *re-arms a daily
        stop that already fired*. A backdated event is evaluated against the current
        day's risk state; the alternative under-counts risk at exactly the moment the
        desk has already lost money.
        """
        if self.market_day is None:
            return replace(self, market_day=market_day)
        if market_day <= self.market_day:
            return self
        return replace(
            self, market_day=market_day, realized_today=ZERO, consecutive_losses=0
        )

    def settled(self, realized: Decimal, market_day: date) -> RiskState:
        """Book one realised result. A flat result breaks a losing streak, a loss extends it."""
        rolled = self.rolled(market_day)
        equity = rolled.equity + realized
        streak = rolled.consecutive_losses + 1 if realized < 0 else 0
        return replace(
            rolled,
            realized_today=rolled.realized_today + realized,
            equity=equity,
            peak_equity=max(rolled.peak_equity, equity),
            consecutive_losses=streak,
        )

    def halted(self, reason: str) -> RiskState:
        """Stand down until a human builds a fresh state. Idempotent on the first reason."""
        if self.halted_reason:
            return self
        return replace(self, halted_reason=reason)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """A multiplier the sizing layer may not exceed, and why."""

    factor: Decimal = ONE
    stand_down: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def reason_text(self) -> str:
        return "|".join(self.reasons) if self.reasons else "risk:full_size"


def decide(
    limits: RiskLimits,
    state: RiskState,
    *,
    market_day: date,
    channel_id: str,
) -> tuple[RiskDecision, RiskState]:
    """Evaluate every limit; return the decision and the state it implies.

    The returned state may carry a new `halted_reason` when a monotone limit is breached -
    callers must persist it, or a restart could forget a stand-down.
    """
    current = state.rolled(market_day)
    factors: list[Decimal] = [ONE]
    reasons: list[str] = []

    if current.halted_reason:
        factors.append(ZERO)
        reasons.append(f"{current.halted_reason} (stand-down until re-armed)")

    realized_r = current.realized_today / limits.risk_unit_usd

    if limits.daily_stop_usd is not None and current.realized_today <= -limits.daily_stop_usd:
        factors.append(ZERO)
        reasons.append(f"daily_stop:{realized_r:.2f}R")

    for threshold, factor in limits.ladder:
        if current.realized_today <= threshold:
            factors.append(factor)
            reasons.append(f"ladder:{threshold}R->x{factor}")

    if limits.max_drawdown_usd is not None and current.drawdown >= limits.max_drawdown_usd:
        factors.append(ZERO)
        reasons.append(f"max_drawdown:{current.drawdown}USD")
        current = current.halted(f"max_drawdown:{current.drawdown}USD")

    for count, factor in limits.loss_streak:
        if current.consecutive_losses >= count:
            factors.append(factor)
            reasons.append(f"streak:{count}->x{factor}(n={current.consecutive_losses})")

    channel = limits.channel_factor(channel_id)
    if channel != ONE:
        factors.append(channel)
        reasons.append(f"channel:{channel_id}->x{channel}")

    factor = _clamp(min(factors))
    return RiskDecision(factor=factor, stand_down=factor <= ZERO, reasons=tuple(reasons)), current


def _clamp(factor: Decimal) -> Decimal:
    """Reduce-only, defensively: never above 1, never below 0, never NaN."""
    if not factor.is_finite():
        return ZERO
    return max(ZERO, min(factor, ONE))


def apply_factor(quantity: int, factor: Decimal) -> int:
    """Shrink an approved contract count. Never raises it, never rounds up, never negative."""
    if quantity <= 0:
        return 0
    safe = _clamp(factor)
    if safe >= ONE:
        return quantity
    return max(0, min(quantity, int((Decimal(quantity) * safe).to_integral_value(
        rounding=ROUND_FLOOR))))

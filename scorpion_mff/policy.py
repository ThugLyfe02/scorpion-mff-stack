"""The risk policy: the only place the desk is allowed to be conservative.

A hot path that sizes correctly is not a desk. A desk has a *written* risk policy: how
much it may lose in a day, what it does after a loss, when it must stand down, and who
signed it. This module is that document, machine-readable and versioned, with one
invariant that everything else leans on:

    THE POLICY IS REDUCE-ONLY.

Every multiplier it can produce is clamped to (0, 1]. The locked clip table in
`sizing.py` is the ceiling; the policy may only shrink the clip. A policy file that
tries to size *up* is rejected at load with a message naming the offending key, rather
than silently clamped - a risk limit that quietly rewrites itself is worse than none.
`governor.apply` clamps again anyway, so a hand-edited policy that bypassed validation
still cannot raise size.

R is the risk unit: one standard clip (`risk_unit_usd`, default the locked $1500
standard clip). Losses and drawdowns are expressed in R so the policy survives a change
of account size - the numbers move with R, the doctrine does not.

Freshness matters as much as content. `stale_after_minutes` makes an unreviewed policy
expire: a risk document nobody has looked at in a week is a rumour, not a limit.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_R_USD = 1500.0
DEFAULT_POLICY_PATH = Path("live/policy.json")
MAX_MULTIPLIER = 1.0
STALE_AFTER_MINUTES = 7 * 24 * 60          # a week: long enough to be useful, short enough to notice


class PolicyError(Exception):
    """The policy cannot be trusted. Never arming on a policy that raised this."""


@dataclass(frozen=True)
class LadderStep:
    """A drawdown/loss threshold and the multiplier that applies once it is reached."""

    at_r: float                 # negative, e.g. -1.0 meaning one risk unit down
    factor: float               # 0.0 = stand down for the rest of the day

    def applies(self, realized_r: float) -> bool:
        return realized_r <= self.at_r


@dataclass(frozen=True)
class StreakStep:
    """After N consecutive losses, cut to `factor` (0.0 = stop until the next day)."""

    at: int
    factor: float

    def applies(self, losses: int) -> bool:
        return losses >= self.at


@dataclass(frozen=True)
class Policy:
    version: int
    name: str
    risk_unit_usd: float = DEFAULT_R_USD
    daily_loss_limit_r: float | None = -2.0
    daily_loss_limit_usd: float | None = None
    max_drawdown_r: float | None = None
    ladder: tuple[LadderStep, ...] = ()
    loss_streak: tuple[StreakStep, ...] = ()
    channel_multipliers: dict[str, float] = field(default_factory=dict)
    session_multipliers: dict[str, float] = field(default_factory=dict)
    stale_after_minutes: int = STALE_AFTER_MINUTES
    written_utc: float = 0.0
    provenance: str = ""

    # ── derived ──────────────────────────────────────────────────────────────
    def daily_loss_limit_usd_effective(self) -> float | None:
        """The tighter of the R-expressed limit and the dollar limit."""
        limits = []
        if self.daily_loss_limit_r is not None:
            limits.append(abs(self.daily_loss_limit_r) * self.risk_unit_usd)
        if self.daily_loss_limit_usd is not None:
            limits.append(abs(self.daily_loss_limit_usd))
        return min(limits) if limits else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version, "name": self.name,
            "risk_unit_usd": self.risk_unit_usd,
            "daily_loss_limit_r": self.daily_loss_limit_r,
            "daily_loss_limit_usd": self.daily_loss_limit_usd,
            "max_drawdown_r": self.max_drawdown_r,
            "ladder": [{"at_r": s.at_r, "factor": s.factor} for s in self.ladder],
            "loss_streak": [{"at": s.at, "factor": s.factor} for s in self.loss_streak],
            "channel_multipliers": dict(self.channel_multipliers),
            "session_multipliers": dict(self.session_multipliers),
            "stale_after_minutes": self.stale_after_minutes,
            "written_utc": self.written_utc,
            "provenance": self.provenance,
        }

    def age_minutes(self, now: float | None = None) -> float | None:
        if not self.written_utc:
            return None
        return ((time.time() if now is None else now) - self.written_utc) / 60.0

    def is_stale(self, now: float | None = None) -> bool:
        age = self.age_minutes(now)
        return age is not None and age > self.stale_after_minutes


def _problems(payload: dict[str, Any]) -> list[str]:
    """Every reason this document is not safe to trade on. All of them, not the first."""
    bad: list[str] = []

    def num(key: str, *, low: float | None = None, high: float | None = None,
            required: bool = True) -> None:
        value = payload.get(key)
        if value is None:
            if required:
                bad.append(f"{key}: required")
            return
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            bad.append(f"{key}: must be a number, got {type(value).__name__}")
            return
        if low is not None and value < low:
            bad.append(f"{key}: {value} is below the minimum {low}")
        if high is not None and value > high:
            bad.append(f"{key}: {value} is above the maximum {high}")

    if payload.get("version") != 1:
        bad.append(f"version: expected 1, got {payload.get('version')!r} (refusing unknown schema)")
    if not str(payload.get("name") or "").strip():
        bad.append("name: required (a policy without a name cannot be reviewed)")

    num("risk_unit_usd", low=1.0)
    num("daily_loss_limit_r", high=0.0, required=False)
    num("daily_loss_limit_usd", high=0.0, required=False)
    num("max_drawdown_r", high=0.0, required=False)
    num("stale_after_minutes", low=1, required=False)

    if payload.get("daily_loss_limit_r") is None and payload.get("daily_loss_limit_usd") is None:
        bad.append("daily_loss_limit_r/daily_loss_limit_usd: at least one hard daily stop is required")

    ladder = payload.get("ladder") or []
    if not isinstance(ladder, list):
        bad.append("ladder: must be a list")
        ladder = []
    previous = 0.0
    for i, step in enumerate(ladder):
        if not isinstance(step, dict):
            bad.append(f"ladder[{i}]: must be an object with at_r and factor")
            continue
        at_r, factor = step.get("at_r"), step.get("factor")
        if not isinstance(at_r, (int, float)) or at_r >= 0:
            bad.append(f"ladder[{i}].at_r: must be negative, got {at_r!r}")
        if not isinstance(factor, (int, float)) or not 0.0 <= factor <= MAX_MULTIPLIER:
            bad.append(f"ladder[{i}].factor: must be within 0..{MAX_MULTIPLIER} "
                       f"(the policy is reduce-only), got {factor!r}")
        if isinstance(at_r, (int, float)) and at_r > previous:
            bad.append(f"ladder[{i}].at_r: must be ordered from 0 downwards, got {at_r}")
        previous = at_r if isinstance(at_r, (int, float)) else previous

    streak = payload.get("loss_streak") or []
    if not isinstance(streak, list):
        bad.append("loss_streak: must be a list")
        streak = []
    for i, step in enumerate(streak):
        if not isinstance(step, dict):
            bad.append(f"loss_streak[{i}]: must be an object with at and factor")
            continue
        at, factor = step.get("at"), step.get("factor")
        if not isinstance(at, int) or at < 1:
            bad.append(f"loss_streak[{i}].at: must be an integer >= 1, got {at!r}")
        if not isinstance(factor, (int, float)) or not 0.0 <= factor <= MAX_MULTIPLIER:
            bad.append(f"loss_streak[{i}].factor: must be within 0..{MAX_MULTIPLIER}, got {factor!r}")

    for key in ("channel_multipliers", "session_multipliers"):
        table = payload.get(key) or {}
        if not isinstance(table, dict):
            bad.append(f"{key}: must be an object")
            continue
        for name, factor in table.items():
            if not isinstance(factor, (int, float)) or isinstance(factor, bool):
                bad.append(f"{key}.{name}: must be a number, got {factor!r}")
            elif not 0.0 <= factor <= MAX_MULTIPLIER:
                bad.append(f"{key}.{name}: {factor} exceeds the locked cap {MAX_MULTIPLIER} "
                           f"- the policy cannot size up, only down")

    if not str(payload.get("provenance") or "").strip():
        bad.append("provenance: required (why this policy exists and who reviewed it)")
    return bad


def parse(payload: dict[str, Any]) -> Policy:
    """Validate then build. Raises with every problem at once."""
    problems = _problems(payload)
    if problems:
        raise PolicyError("policy rejected:\n  - " + "\n  - ".join(problems))
    return Policy(
        version=int(payload["version"]),
        name=str(payload["name"]),
        risk_unit_usd=float(payload.get("risk_unit_usd", DEFAULT_R_USD)),
        daily_loss_limit_r=payload.get("daily_loss_limit_r"),
        daily_loss_limit_usd=payload.get("daily_loss_limit_usd"),
        max_drawdown_r=payload.get("max_drawdown_r"),
        ladder=tuple(LadderStep(float(s["at_r"]), float(s["factor"]))
                     for s in payload.get("ladder") or ()),
        loss_streak=tuple(StreakStep(int(s["at"]), float(s["factor"]))
                          for s in payload.get("loss_streak") or ()),
        channel_multipliers={str(k): float(v) for k, v in
                             (payload.get("channel_multipliers") or {}).items()},
        session_multipliers={str(k): float(v) for k, v in
                             (payload.get("session_multipliers") or {}).items()},
        stale_after_minutes=int(payload.get("stale_after_minutes", STALE_AFTER_MINUTES)),
        written_utc=float(payload.get("written_utc") or 0.0),
        provenance=str(payload.get("provenance", "")),
    )


def load(path: str | Path = DEFAULT_POLICY_PATH) -> Policy:
    """Read and validate. A missing or unreadable policy is an error, never a default."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PolicyError(f"no policy at {p} - the desk does not trade without one") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot read policy at {p}: {exc}") from None
    if not isinstance(raw, dict):
        raise PolicyError(f"policy at {p} must be a JSON object")
    return parse(raw)


def write(policy: Policy, path: str | Path = DEFAULT_POLICY_PATH) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(policy.to_dict(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return p


def baseline(name: str = "baseline-v1", *, written_utc: float | None = None,
             provenance: str = "hand-written; derived from BACKTEST.md, review before arming",
             **overrides) -> Policy:
    """The starting policy. Deliberately mild: it must not change the corpus result much.

    The ladder is expressed in R and checked against the corpus in `policy simulate`,
    so the first version is a measured choice rather than a nervous one.
    """
    payload: dict[str, Any] = {
        "version": 1,
        "name": name,
        "risk_unit_usd": DEFAULT_R_USD,
        "daily_loss_limit_r": -2.0,
        "daily_loss_limit_usd": None,
        "max_drawdown_r": -6.0,
        "ladder": [
            {"at_r": -1.0, "factor": 0.5},
            {"at_r": -2.0, "factor": 0.0},
        ],
        "loss_streak": [
            {"at": 2, "factor": 0.5},
            {"at": 3, "factor": 0.0},
        ],
        "channel_multipliers": {},
        "session_multipliers": {},
        "stale_after_minutes": STALE_AFTER_MINUTES,
        "written_utc": time.time() if written_utc is None else written_utc,
        "provenance": provenance,
    }
    payload.update(overrides)
    return parse(payload)


def summarise(policy: Policy, now: float | None = None) -> str:
    """The one-screen view an operator reads before arming."""
    lines = [
        f"policy {policy.name}  (schema v{policy.version})",
        f"  written    : {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(policy.written_utc))}"
        if policy.written_utc else "  written    : (undated)",
        f"  risk unit  : ${policy.risk_unit_usd:,.0f}  (1R)",
        f"  daily stop : {policy.daily_loss_limit_r if policy.daily_loss_limit_r is not None else '-'}R"
        f" / ${policy.daily_loss_limit_usd_effective() or 0:,.0f}",
    ]
    if policy.max_drawdown_r is not None:
        lines.append(f"  max DD     : {policy.max_drawdown_r}R")
    if policy.ladder:
        lines.append("  drawdown ladder: " + ", ".join(
            f"{s.at_r:+.1f}R -> x{s.factor:g}" for s in policy.ladder))
    if policy.loss_streak:
        lines.append("  loss streak    : " + ", ".join(
            f"{s.at} -> x{s.factor:g}" for s in policy.loss_streak))
    for label, table in (("channel", policy.channel_multipliers),
                         ("session", policy.session_multipliers)):
        if table:
            lines.append(f"  {label} multipliers: " + ", ".join(
                f"{k} x{v:g}" for k, v in table.items()))
    age = policy.age_minutes(now)
    lines.append(f"  age        : {'never dated' if age is None else f'{age / 60:.1f} h'}"
                 + ("  ** STALE: review before arming **" if policy.is_stale(now) else ""))
    lines.append(f"  provenance : {policy.provenance}")
    lines.append("  invariant  : reduce-only - no multiplier above x1.0 can be expressed")
    return "\n".join(lines)

"""Pre-trade gate: the point where measurements become refusals.

`drift`, `rough`, `tails` and `hawkes` all produce reports, and a report nobody acts on is a
liability with a maintenance cost. This module is the seam: each check returns a named verdict, the
gate composes them, and a refusal is as auditable as a pass.

Two rules make it fail closed rather than look reassuring:

  * an UNEVALUABLE check is a refusal, not a pass. A missing measurement is not evidence of safety,
    and the commonest way a gate rots is by treating silence as consent.
  * every check runs and every verdict is reported. Short-circuiting on the first refusal hides that
    three other things were also wrong, which is what an operator needs to know.

The context is a plain dict of measured values, so the gate works before every upstream module
is wired and a missing key is an explicit unevaluable rather than an AttributeError.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class GuardError(ValueError):
    """A gate configuration that cannot be evaluated, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


PASS = "pass"
REFUSE = "refuse"
UNEVALUABLE = "unevaluable"


@dataclass(frozen=True, slots=True)
class Verdict:
    check: str
    outcome: str
    detail: str
    value: float | None = None

    @property
    def blocks(self) -> bool:
        return self.outcome in (REFUSE, UNEVALUABLE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "outcome": self.outcome,
            "detail": self.detail,
            "value": None if self.value is None else round(self.value, 6),
            "blocks": self.blocks,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    verdicts: tuple[Verdict, ...] = field(default_factory=tuple)

    @property
    def blocking(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.blocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "blocking": [v.check for v in self.blocking],
        }


def _number(context: Mapping[str, Any], key: str) -> float:
    if key not in context or context[key] is None:
        raise GuardError("missing", key)
    return float(context[key])


def check_drift(context: Mapping[str, Any]) -> Verdict:
    """Refuse when the recent outcome window is not from the corpus's world."""
    drifted = context.get("drifted")
    if drifted is None:
        return Verdict("drift", UNEVALUABLE, "no drift measurement supplied")
    distance = context.get("drift_distance")
    if drifted:
        return Verdict(
            "drift", REFUSE,
            "recent outcomes out of distribution; reweighting would treat decay as adaptation",
            None if distance is None else float(distance),
        )
    return Verdict(
        "drift", PASS, "outcome window in distribution",
        None if distance is None else float(distance),
    )


def check_vol_estimate(context: Mapping[str, Any]) -> Verdict:
    """Refuse when the volatility estimate is older than its own memory length."""
    age = _number(context, "vol_age_seconds")
    memory = _number(context, "vol_memory_seconds")
    if memory <= 0:
        raise GuardError("bad_memory", str(memory))
    if age > memory:
        return Verdict(
            "vol_estimate", REFUSE,
            f"estimate is {age:.0f}s old against a {memory:.0f}s memory length",
            age,
        )
    return Verdict("vol_estimate", PASS, "estimate is fresh against its memory length", age)


def check_tail_size(context: Mapping[str, Any]) -> Verdict:
    """Refuse a size whose tail-implied loss probability exceeds the desk's tolerance."""
    probability = _number(context, "tail_probability")
    tolerance = _number(context, "tail_tolerance")
    if tolerance <= 0:
        raise GuardError("bad_tolerance", str(tolerance))
    if probability > tolerance:
        return Verdict(
            "tail_size", REFUSE,
            f"P(loss beyond clip) {probability:.4f} exceeds tolerance {tolerance:.4f}",
            probability,
        )
    return Verdict("tail_size", PASS, "clip sits inside the tail tolerance", probability)


def check_burst(context: Mapping[str, Any]) -> Verdict:
    """Refuse when clustering is high enough that the book is full for arithmetic reasons."""
    branching = _number(context, "branching_ratio")
    limit = _number(context, "branching_limit")
    blocked_rate = context.get("blocked_rate")
    if branching > limit:
        detail = (
            f"branching {branching:.2f} above limit {limit:.2f}: bursts, not opportunity, "
            "are filling the book"
        )
        if blocked_rate is not None:
            detail += f" (cap met on {float(blocked_rate):.1%} of arrivals)"
        return Verdict("burst", REFUSE, detail, branching)
    return Verdict("burst", PASS, "arrival clustering within limit", branching)


def check_cooldown(context: Mapping[str, Any]) -> Verdict:
    """Refuse while a halt is still inside its cooldown."""
    elapsed = _number(context, "since_halt_seconds")
    required = _number(context, "cooldown_seconds")
    if required < 0:
        raise GuardError("bad_cooldown", str(required))
    if elapsed < required:
        return Verdict(
            "cooldown", REFUSE,
            f"{elapsed:.0f}s since the last halt, cooldown is {required:.0f}s", elapsed,
        )
    return Verdict("cooldown", PASS, "cooldown satisfied", elapsed)


DEFAULT_CHECKS: tuple[Callable[[Mapping[str, Any]], Verdict], ...] = (
    check_cooldown,
    check_drift,
    check_vol_estimate,
    check_tail_size,
    check_burst,
)


def evaluate(
    context: Mapping[str, Any],
    checks: Sequence[Callable[[Mapping[str, Any]], Verdict]] | None = None,
) -> Decision:
    """Run every check; any block refuses, and an exception inside one becomes unevaluable."""
    active = checks if checks is not None else DEFAULT_CHECKS
    if not active:
        raise GuardError("no_checks", "a gate with no checks refuses by definition")
    verdicts: list[Verdict] = []
    for check in active:
        try:
            verdicts.append(check(context))
        except GuardError as error:
            verdicts.append(
                Verdict(getattr(check, "__name__", "check"), UNEVALUABLE, error.detail)
            )
        except (KeyError, TypeError, ValueError) as error:
            verdicts.append(
                Verdict(getattr(check, "__name__", "check"), UNEVALUABLE, str(error))
            )
    return Decision(allowed=not any(v.blocks for v in verdicts), verdicts=tuple(verdicts))


def audit_record(
    decision: Decision, *, contract_key: str, size_usd: float, as_of: str,
) -> dict[str, Any]:
    """The row a post-mortem reads: what was decided, on what evidence, at what size."""
    if not contract_key:
        raise GuardError("no_contract", "an audit record needs the contract it concerns")
    return {
        "contract_key": contract_key,
        "size_usd": round(size_usd, 2),
        "as_of": as_of,
        "allowed": decision.allowed,
        "verdicts": [v.to_dict() for v in decision.verdicts],
        "blocking": [v.check for v in decision.blocking],
        "refused": not decision.allowed,
    }


def render(decision: Decision) -> str:
    lines = ["allowed" if decision.allowed else "refused"]
    for verdict in decision.verdicts:
        mark = "x" if verdict.blocks else "-"
        lines.append(f"  [{mark}] {verdict.check:14s} {verdict.outcome:12s} {verdict.detail}")
    return "\n".join(lines)

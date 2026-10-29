"""Lifecycles: turning "sold half at 25%, runners to 75%" into an ordered, checkable plan.

The book models one entry and one exit. The trader does not. His own record says things like
"trimmed half at 25%, sold remaining at 75%", "added at .57/.65", "runners to 100%". Every
analysis in this repository collapses that into a single number, which is why the corpus can report
a mean R but cannot answer "how much did the second half earn" or "was scaling out better than
holding".

This module makes the sequence first-class, with three invariants that matter more than the parsing:

1. **Nothing can be sold twice.** The sum of every intent is exactly the entry quantity, and the
   final step absorbs the remainder rather than rounding it away.
2. **Nothing can be sold that is not held.** Each intent is bounded by what remains at that step.
3. **A plan is monotone in trigger.** Steps fire in increasing order of the multiple that unlocks
   them, so a plan can never ask to sell at +25% after asking to sell at +75%.

Deliberately NOT here: deciding whether scaling out is good. That is a research question the plan
makes answerable, and `ruin.py` already demonstrates the house style for answering it - measure,
then withhold when the sample cannot support the claim.

Parsing is intentionally conservative. A claim it cannot read becomes `unparsed` with the text kept,
because a lifecycle that silently becomes "sell everything at the end" is a lifecycle that flatters
every trailing-stop strategy ever tested.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

D = Decimal
_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_FRACTION_WORDS = {
    "half": D("0.5"), "third": D("0.3333"), "quarter": D("0.25"), "two-thirds": D("0.6667"),
}


class LifecycleError(ValueError):
    """A plan that cannot hold together, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Step:
    """One sale in a lifecycle. `fraction` is of the ORIGINAL entry quantity."""

    fraction: D
    at_multiple: D | None = None      # premium multiple that triggers it, e.g. 1.25
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fraction": str(self.fraction),
            "at_multiple": str(self.at_multiple) if self.at_multiple is not None else None,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class Intent:
    """A concrete, integer sale: what the executor would actually send."""

    contracts: int
    at_multiple: D | None
    label: str
    cumulative: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "contracts": self.contracts,
            "at_multiple": str(self.at_multiple) if self.at_multiple is not None else None,
            "label": self.label,
            "cumulative": self.cumulative,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    quantity: int
    steps: tuple[Step, ...]
    intent: str = ""                   # the claim this was derived from
    unparsed: bool = False

    @property
    def total_fraction(self) -> D:
        return sum((step.fraction for step in self.steps), D(0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity, "intent": self.intent, "unparsed": self.unparsed,
            "steps": [s.to_dict() for s in self.steps],
            "total_fraction": str(self.total_fraction),
        }


def single_exit(quantity: int, *, label: str = "single") -> Plan:
    return Plan(quantity=quantity, steps=(Step(D(1), None, label),))


def _normalise(quantity: int, steps: Sequence[Step]) -> tuple[Step, ...]:
    """Scale fractions so they sum to exactly 1, keeping their relative sizes.

    A claim like "half at 25%, runners at 75%" does not say what the runner is; normalising makes
    the plan executable without inventing a number, and the final step always absorbs the
    rounding - which is the property that stops a fraction of a contract from vanishing.
    """
    total = sum((step.fraction for step in steps), D(0))
    if total <= 0:
        raise LifecycleError("empty_plan", "steps sum to zero")
    if abs(total - D(1)) < D("0.0001"):
        return tuple(steps)
    scaled = tuple(
        Step(step.fraction / total, step.at_multiple, step.label) for step in steps
    )
    return scaled


def intents(plan: Plan) -> tuple[Intent, ...]:
    """Integer contracts per step: floors everywhere, remainder absorbed by the last step."""
    if plan.quantity < 1:
        raise LifecycleError("no_quantity", f"quantity {plan.quantity}")
    if not plan.steps:
        raise LifecycleError("empty_plan", "no steps")
    steps = _normalise(plan.quantity, plan.steps)

    out: list[Intent] = []
    sold = 0
    for index, step in enumerate(steps):
        remaining = plan.quantity - sold
        if index == len(steps) - 1:
            contracts = remaining
        else:
            contracts = int((D(plan.quantity) * step.fraction).to_integral_value(ROUND_DOWN))
            contracts = max(0, min(contracts, remaining))
        if contracts == 0 and index < len(steps) - 1:
            continue                     # a clip too small to sell; the final step absorbs it
        sold += contracts
        out.append(Intent(contracts, step.at_multiple, step.label, cumulative=sold))

    if sold != plan.quantity:             # pragma: no cover - guarded by the final-step absorb
        raise LifecycleError("quantity_leak", f"sold {sold} of {plan.quantity}")
    return tuple(out)


def validate(plan: Plan) -> list[str]:
    """Audit a plan. Returns problems; empty means the plan can be executed as written."""
    problems: list[str] = []
    if plan.quantity < 1:
        problems.append("quantity must be at least 1")
    if not plan.steps:
        problems.append("plan has no steps")
        return problems
    if any(step.fraction < 0 for step in plan.steps):
        problems.append("a step has a negative fraction")
    triggers = [s.at_multiple for s in plan.steps if s.at_multiple is not None]
    if triggers != sorted(triggers):
        problems.append("triggers are not monotone: a later step fires earlier than a prior one")
    try:
        resolved = intents(plan)
    except LifecycleError as exc:
        return [*problems, f"{exc.code}: {exc.detail}"]
    if sum(i.contracts for i in resolved) != plan.quantity:
        problems.append("intents do not sum to the entry quantity")
    if any(i.contracts <= 0 for i in resolved):
        problems.append("a resolved step sells zero contracts")
    return problems


# ── parsing a claim ─────────────────────────────────────────────────────────
_TRIM_WORDS = ("trim", "scale out", "scaled out", "sell half", "sold half", "take half",
               "sell part", "partial", "runner", "runners", "remaining", "rest")


def from_claim(quantity: int, text: str, *, exit_pct: int | None = None) -> Plan:
    """Derive a plan from the trader's own words, or return an honest `unparsed` plan.

    Two shapes are supported because they are the two the corpus actually contains:

      * a scale-out claim: "sold half at 25%, runners to 75%" -> two steps, normalised;
      * a single claimed exit percentage: "+75%" -> one step at the end.

    Anything else is `unparsed` with the text preserved. A plan that guesses is worse than a plan
    that says it could not read the claim, because the guess silently becomes a strategy result.
    """
    raw = (text or "").strip()
    if not raw:
        if exit_pct is None:
            return Plan(quantity=quantity, steps=(Step(D(1), None, "no_claim"),), intent="",
                        unparsed=True)
        step = Step(D(1), D(1 + D(exit_pct) / 100), "claimed")
        return Plan(quantity=quantity, steps=(step,), intent=raw)

    lowered = raw.lower()
    has_trim = any(word in lowered for word in _TRIM_WORDS)
    percentages = [D(m.group(1)) for m in _PCT.finditer(raw)]

    if not has_trim:
        multiple = None
        if exit_pct is not None:
            multiple = D(1 + D(exit_pct) / 100)
        elif percentages:
            multiple = D(1 + percentages[-1] / 100)
        return Plan(quantity=quantity, steps=(Step(D(1), multiple, "single"),), intent=raw)

    weights: list[Step] = []
    for word, fraction in _FRACTION_WORDS.items():
        if word in lowered:
            weights.append(Step(fraction, None, word))

    if not weights:
        # "trimmed at 25%, rest at 75%": two unnamed clips; split evenly and say so in the label
        count = max(2, len(percentages))
        weights = [Step(D(1) / D(count), None, f"clip{i + 1}") for i in range(count)]
    elif len(weights) < len(percentages):
        # "sold half at 25%, runners to 75%": the named fraction covers part of the position and
        # the rest is implied by the second target. Without this the runner silently disappears
        # and the whole position sells at the first trigger - the most flattering possible reading.
        covered = sum((step.fraction for step in weights), D(0))
        if covered < 1:
            weights.append(Step(D(1) - covered, None, "runner"))

    if percentages:
        # pair each clip with the stated target, in order of appearance
        weights = [
            Step(step.fraction, D(1 + percentages[i] / 100) if i < len(percentages) else None,
                 step.label)
            for i, step in enumerate(weights)
        ]
    # Prose order is not execution order. The corpus contains claims written "35% ... 30% ... 100%"
    # where the stated targets descend; a lifecycle fires by TRIGGER, so steps are ordered by it.
    # Without this, two shipped trades produced plans that could never execute in the order given.
    ordered = sorted(
        weights, key=lambda step: (step.at_multiple is None, step.at_multiple or D(0))
    )
    return Plan(quantity=quantity, steps=tuple(ordered), intent=raw)


def from_ledger_trade(trade: Any) -> Plan:
    """The plan implied by one corpus trade."""
    quantity = 1                          # the corpus records one clip per row
    notes = getattr(trade, "notes", "") or ""
    exit_pct = getattr(trade.lifecycle, "exit_pct", None)
    return from_claim(quantity, notes, exit_pct=exit_pct)


def describe(plan: Plan) -> str:
    parts = [f"{i.contracts}x" for i in intents(plan)]
    triggers = [
        f"@{i.at_multiple}" for i in intents(plan) if i.at_multiple is not None
    ]
    return f"{plan.quantity} contracts as {' + '.join(parts)}" + (
        f" at {', '.join(triggers)}" if triggers else ""
    )


def summary(plans: Iterable[Plan]) -> dict[str, Any]:
    total = unparsed = multi = 0
    for plan in plans:
        total += 1
        unparsed += 1 if plan.unparsed else 0
        multi += 1 if len(plan.steps) > 1 else 0
    return {
        "plans": total,
        "single_step": total - multi,
        "multi_step": multi,
        "unparsed": unparsed,
        "parse_rate": round(100.0 * (total - unparsed) / total, 1) if total else 0.0,
    }

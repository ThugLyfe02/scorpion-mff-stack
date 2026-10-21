"""Adaptive rule weighting: keep every rule, let the evidence decide.

Selecting the best rule from a backtest is a bet that the past ranks the future. That bet fails
exactly when it matters - when the edge decays or the regime shifts - and the audit machinery for it
measures the damage after the fact rather than avoiding it.

This does something different. Every rule keeps a weight. Weights move by realised loss, so a rule
that is working earns more capital and one that stops working loses it, with no re-fit and no
selection event. The guarantee is a regret bound: cumulative loss stays within O(sqrt(T log K)) of
the best single rule in hindsight, for any loss sequence - adversarial, non-stationary, or
regime-switching.

The update is exponential weights with a fixed share, which keeps the bound when the best rule
CHANGES rather than merely being unknown:

    w_i <- (1 - alpha) * w_i * exp(-eta * loss_i) + alpha / K

eta comes from the horizon (sqrt(8 ln K / T)); alpha reserves a floor for every rule so a recovered
rule can regain weight, at a bounded cost in regret.

Two refusals keep this honest: no losses means uniform weights, because an unplayed rule is not a
good rule; and every weight change is reported with the loss that caused it, so adaptation is
auditable rather than an unexplained drift in allocation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class ExpertsError(ValueError):
    """A weighting problem that cannot be solved, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def learning_rate(n_rounds: int, n_experts: int) -> float:
    """The Hedge rate: sqrt(8 ln K / T), floored so a short history is not over-reactive."""
    if n_rounds < 1 or n_experts < 1:
        raise ExpertsError("bad_shape", f"rounds={n_rounds} experts={n_experts}")
    return min(1.0, math.sqrt(8.0 * math.log(max(2, n_experts)) / n_rounds))


def regret_bound(n_rounds: int, n_experts: int) -> float:
    """Worst-case regret against the best single expert, in total loss units."""
    if n_rounds < 1 or n_experts < 1:
        raise ExpertsError("bad_shape", f"rounds={n_rounds} experts={n_experts}")
    return math.sqrt(n_rounds * math.log(max(2, n_experts)) / 2.0)


@dataclass
class Hedge:
    """Exponential weights over K rules, with a fixed share for recovery."""

    n_experts: int
    eta: float = 0.0
    alpha: float = 0.02
    weights: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.n_experts < 1:
            raise ExpertsError("no_experts", str(self.n_experts))
        if not (0.0 <= self.alpha < 1.0):
            raise ExpertsError("bad_alpha", str(self.alpha))
        if not self.weights:
            self.weights = [1.0 / self.n_experts] * self.n_experts
        if len(self.weights) != self.n_experts:
            raise ExpertsError("bad_weights", f"{len(self.weights)} for {self.n_experts}")

    def update(self, losses: Sequence[float]) -> list[float]:
        """One round: reweight by exp(-eta * loss) with a fixed share added back."""
        if len(losses) != self.n_experts:
            raise ExpertsError("bad_losses", f"{len(losses)} for {self.n_experts}")
        if self.eta <= 0:
            raise ExpertsError("no_rate", "eta must be positive before an update")
        share = self.alpha / self.n_experts
        raw = [
            (1.0 - self.alpha) * w * math.exp(-self.eta * loss)
            for w, loss in zip(self.weights, losses, strict=False)
        ]
        total = sum(raw)
        if total <= 0 or not math.isfinite(total):
            # underflow or a runaway loss: fall back to uniform rather than NaN weights
            self.weights = [1.0 / self.n_experts] * self.n_experts
            return list(self.weights)
        self.weights = [value / total + share for value in raw]
        self.weights = [value / sum(self.weights) for value in self.weights]
        return list(self.weights)

    def leader(self) -> int:
        return max(range(self.n_experts), key=lambda i: self.weights[i])

    def concentration(self) -> float:
        """Herfindahl: 1/K is uniform, 1.0 is total commitment to one rule."""
        return sum(w * w for w in self.weights)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": [round(w, 4) for w in self.weights],
            "leader": self.leader(),
            "concentration": round(self.concentration(), 4),
            "eta": round(self.eta, 5),
            "alpha": self.alpha,
        }


@dataclass(frozen=True, slots=True)
class Round:
    index: int
    losses: tuple[float, ...]
    weights: tuple[float, ...]
    leader: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "losses": [round(x, 4) for x in self.losses],
            "weights": [round(x, 4) for x in self.weights],
            "leader": self.leader,
        }


@dataclass(frozen=True, slots=True)
class Result:
    names: tuple[str, ...]
    rounds: tuple[Round, ...]
    cumulative: tuple[float, ...]
    blended_cumulative: float
    best_expert: int
    regret: float
    bound: float

    @property
    def within_bound(self) -> bool:
        return self.regret <= self.bound + 1e-9

    def to_dict(self) -> dict[str, Any]:
        return {
            "names": list(self.names),
            "rounds": len(self.rounds),
            "cumulative_loss": [round(x, 4) for x in self.cumulative],
            "blended_cumulative_loss": round(self.blended_cumulative, 4),
            "best_expert": self.names[self.best_expert] if self.names else None,
            "regret": round(self.regret, 4),
            "bound": round(self.bound, 4),
            "within_bound": self.within_bound,
            "final_weights": list(self.rounds[-1].weights) if self.rounds else [],
        }


def run(
    losses_by_expert: Mapping[str, Sequence[float]], *, alpha: float = 0.02,
    eta: float | None = None,
) -> Result:
    """Blend experts over a loss sequence and report the regret against the best in hindsight."""
    names = tuple(losses_by_expert)
    if not names:
        raise ExpertsError("no_experts", "empty mapping")
    series = [list(losses_by_expert[name]) for name in names]
    lengths = {len(s) for s in series}
    if len(lengths) != 1:
        raise ExpertsError("ragged", f"expert series lengths {sorted(lengths)}")
    rounds_count = lengths.pop()
    if rounds_count == 0:
        raise ExpertsError("no_rounds", "every series is empty")

    rate = eta if eta is not None else learning_rate(rounds_count, len(names))
    hedge = Hedge(n_experts=len(names), eta=rate, alpha=alpha)

    rounds: list[Round] = []
    blended = 0.0
    for index in range(rounds_count):
        vector = tuple(series[i][index] for i in range(len(names)))
        blended += sum(w * loss for w, loss in zip(hedge.weights, vector, strict=False))
        weights = hedge.update(vector)
        rounds.append(Round(index, vector, tuple(weights), hedge.leader()))

    cumulative = tuple(sum(s) for s in series)
    best = min(range(len(names)), key=lambda i: cumulative[i])
    return Result(
        names=names, rounds=tuple(rounds), cumulative=cumulative,
        blended_cumulative=blended, best_expert=best,
        regret=blended - cumulative[best], bound=regret_bound(rounds_count, len(names)),
    )


def from_returns(returns_by_expert: Mapping[str, Sequence[float]], **kwargs: Any) -> Result:
    """Rules are scored on realised R; loss is its negative, so better rules accrue less."""
    return run({name: [-r for r in series] for name, series in returns_by_expert.items()}, **kwargs)


def weight_path(result: Result) -> dict[str, list[float]]:
    """Each rule's weight over time: the most honest picture of what the data supports."""
    return {
        name: [round(rnd.weights[i], 4) for rnd in result.rounds]
        for i, name in enumerate(result.names)
    }


def switches(result: Result, *, threshold: float = 0.1) -> list[dict[str, Any]]:
    """Rounds where the leader changed by more than `threshold` of weight mass."""
    out: list[dict[str, Any]] = []
    previous = result.rounds[0].leader if result.rounds else 0
    for rnd in result.rounds[1:]:
        if rnd.leader != previous:
            moved = rnd.weights[rnd.leader] - rnd.weights[previous]
            if abs(moved) >= threshold:
                out.append({
                    "round": rnd.index, "from": result.names[previous],
                    "to": result.names[rnd.leader], "moved": round(moved, 4),
                })
            previous = rnd.leader
    return out


def report(result: Result) -> dict[str, Any]:
    return {
        **result.to_dict(),
        "weight_path": weight_path(result),
        "switches": switches(result),
        "caveats": [
            "the regret bound holds for any loss sequence, so it survives regime change and "
            "adversarial data - it does not promise profit, only that no single rule would have "
            "done much better",
            "weights are an allocation, not a prediction; the leader can be wrong next round",
            "a fixed share is held for every rule so a recovered rule can regain weight",
        ],
    }


def render(payload: Mapping[str, Any]) -> str:
    names = payload["names"]
    final = payload["final_weights"]
    lines = [f"{payload['rounds']} rounds over {len(names)} rules"]
    for name, weight, loss in zip(names, final, payload["cumulative_loss"], strict=False):
        lines.append(f"  {name:22s} weight {weight:.3f}   cumulative loss {loss:+.2f}")
    lines.append(
        f"  best in hindsight {payload['best_expert']}  regret {payload['regret']:+.3f} "
        f"(bound {payload['bound']:.3f}, within={payload['within_bound']})"
    )
    for change in payload["switches"][:5]:
        lines.append(
            f"  switch at {change['round']}: {change['from']} -> {change['to']}"
        )
    return "\n".join(lines)


def adaptive(
    returns_by_expert: Iterable[tuple[str, Sequence[float]]], **kwargs: Any,
) -> dict[str, Any]:
    return report(from_returns(dict(returns_by_expert), **kwargs))

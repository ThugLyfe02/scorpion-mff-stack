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


# ── wiring the runner into the allocator ────────────────────────────────────
# `corpus_replay.report()` already emits one row per trade per variant, each carrying
# `executed_r`. That is exactly the loss matrix a blending rule needs, so the adaptation
# does not require a new data path - only alignment and a shape change.

def aligned_returns(
    trades_by_variant: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, list[float]], list[str]]:
    """Align per-variant trade rows on identity and return (returns, identities).

    A rule that refused a trade it should have taken has no R for it; treating that as zero
    would reward refusal during drawdowns, so a missing trade is dropped for that round and
    the count is reported by `coverage`. Identities are the intersection across variants,
    which is the only set on which a comparison is apples to apples.
    """
    per_variant: dict[str, dict[str, float]] = {}
    for name, rows in trades_by_variant.items():
        series: dict[str, float] = {}
        for row in rows:
            identity = str(row.get("identity", ""))
            value = row.get("executed_r")
            if identity and value is not None:
                series[identity] = float(value)
        per_variant[name] = series

    if not per_variant:
        raise ExpertsError("no_variants", "nothing to blend")
    shared = set(next(iter(per_variant.values())))
    for series in per_variant.values():
        shared &= set(series)
    identities = sorted(shared)
    if not identities:
        # alignment reports what exists; deciding whether a blend is meaningful is the caller's job
        raise ExpertsError("thin_overlap", "no trade executed by every variant")
    returns = {name: [series[i] for i in identities] for name, series in per_variant.items()}
    return returns, identities


def from_variant_trades(
    trades_by_variant: Mapping[str, Sequence[Mapping[str, Any]]], **kwargs: Any,
) -> dict[str, Any]:
    """Blend the attribution variants over their shared trades and report the allocation."""
    returns, identities = aligned_returns(trades_by_variant)
    result = from_returns(returns, **kwargs)
    payload = report(result)
    payload["identity_count"] = len(identities)
    payload["variant_count"] = len(returns)
    payload["coverage_note"] = (
        "blended over the intersection of trades every variant executed; a variant that "
        "refused a trade contributes nothing to that round"
    )
    return payload


def weights_file(payload: Mapping[str, Any]) -> dict[str, float]:
    """The allocation a sizing layer would consume: rule name to weight, summing to one."""
    names = payload["names"]
    final = payload["final_weights"]
    return {name: float(weight) for name, weight in zip(names, final, strict=False)}


def allocation_drift(first: Mapping[str, Any], last: Mapping[str, Any]) -> dict[str, Any]:
    """How far the allocation moved between two runs - the size of the adaptation."""
    a = weights_file(first)
    b = weights_file(last)
    common = sorted(set(a) & set(b))
    drift = {name: round(b[name] - a[name], 4) for name in common}
    total = sum(abs(v) for v in drift.values())
    return {
        "drift": drift,
        "total_variation": round(total / 2.0, 4),
        "largest_move": (
            max(drift, key=lambda k: abs(drift[k])) if drift else None
        ),
    }


# ── contextual weighting: the same rules, different weights per condition ───
# A single weight vector assumes one best rule for all conditions. The corpus says otherwise: the
# premium-band work found the edge concentrated in one band, and the regime work found sessions that
# do not resemble each other. So the allocator runs one Hedge PER CONTEXT and asks the current
# context which allocator to listen to. This is the sleeping-experts construction, restricted to a
# partition we can observe rather than one we have to infer.

def by_context(
    returns_by_expert: Mapping[str, Sequence[float]],
    contexts: Sequence[str],
    **kwargs: Any,
) -> dict[str, Result]:
    """One blended allocator per context. Returns name -> Result."""
    names = list(returns_by_expert)
    if not names:
        raise ExpertsError("no_experts", "empty mapping")
    lengths = {len(returns_by_expert[n]) for n in names}
    if len(lengths) != 1:
        raise ExpertsError("ragged", f"expert series lengths {sorted(lengths)}")
    rounds = lengths.pop()
    if len(contexts) != rounds:
        raise ExpertsError("context_mismatch", f"{len(contexts)} contexts for {rounds} rounds")

    buckets: dict[str, list[int]] = {}
    for index, context in enumerate(contexts):
        buckets.setdefault(str(context), []).append(index)

    out: dict[str, Result] = {}
    for context, indices in sorted(buckets.items()):
        if len(indices) < 2:
            raise ExpertsError(
                "thin_context", f"context {context!r} has {len(indices)} round(s)"
            )
        subset = {
            name: [returns_by_expert[name][i] for i in indices] for name in names
        }
        out[context] = from_returns(subset, **kwargs)
    return out


def advise(
    contextual: Mapping[str, Result], context: str, *,
    fallback: Result | None = None,
) -> dict[str, Any]:
    """The allocation to use right now, given the current context.

    Falls back to the pooled allocator when a context has never been seen, and says so - silently
    using uniform weights would look identical to a considered decision.
    """
    if context in contextual:
        payload = report(contextual[context])
        payload["source"] = "context"
        payload["context"] = context
        return payload
    if fallback is None:
        raise ExpertsError("unknown_context", f"{context!r} was never observed")
    payload = report(fallback)
    payload["source"] = "pooled_fallback"
    payload["context"] = context
    return payload


def sizes_from_weights(
    weights: Mapping[str, float], *, budget: float, floor: float = 0.0, cap: float = 1.0,
) -> dict[str, float]:
    """Turn an allocation into dollar risk per rule, with a floor and a concentration cap.

    Weights are a share of attention; sizes are a share of risk. A rule that has never traded has
    no business consuming budget, so anything at or below `floor` is set to zero and the remainder
    is renormalised. The cap stops one rule from eating the book when the others go quiet.
    """
    if budget <= 0:
        raise ExpertsError("bad_budget", str(budget))
    if not (0.0 <= floor < 1.0):
        raise ExpertsError("bad_floor", str(floor))
    if not (0.0 < cap <= 1.0):
        raise ExpertsError("bad_cap", str(cap))

    kept = {name: w for name, w in weights.items() if w > floor}
    if not kept:
        return {name: 0.0 for name in weights}
    total = sum(kept.values())
    scaled = {name: value / total for name, value in kept.items()}

    # Water-filling, not a single clip: capping and renormalising in one pass undoes the cap,
    # because the excess flows straight back to the rule that was capped. So capped rules are
    # fixed at the cap and the remaining mass is redistributed among the rest until nothing binds.
    final = {name: 0.0 for name in weights}
    remaining = dict(scaled)
    free = 1.0
    for _ in range(len(scaled) + 1):
        pool = sum(remaining.values())
        if not remaining or pool <= 0:
            break
        breached = [n for n, v in remaining.items() if v / pool * free > cap]
        if not breached:
            for name, value in remaining.items():
                final[name] = value / pool * free
            break
        for name in breached:
            final[name] = cap
            free -= cap
            remaining.pop(name)
    return {name: round(final.get(name, 0.0) * budget, 2) for name in weights}


def posture(weights: Mapping[str, float], *, budget: float, floor: float = 0.05,
            cap: float = 0.6) -> dict[str, Any]:
    """A one-call answer for the sizing layer: sizes, concentration, and whether it is decisive."""
    allocated = sizes_from_weights(weights, budget=budget, floor=floor, cap=cap)
    live = {name: value for name, value in allocated.items() if value > 0}
    concentration = sum((v / budget) ** 2 for v in live.values()) if budget else 0.0
    return {
        "sizes": allocated,
        "live_rules": len(live),
        "concentration": round(concentration, 4),
        "decisive": bool(live) and concentration >= 1.5 / max(1, len(allocated)),
        "note": (
            "sizes are risk budget shares, not contract counts; the sizing layer converts them "
            "using the premium band the rule traded in"
        ),
    }


# ── variance-aware weighting: a noisy rule has not earned conviction ───────
# Plain Hedge compares raw losses, so a rule that had one huge win looks identical to one that
# grinds out small wins - and the first is far more likely to be luck. Empirical-Bernstein
# weighting scores each rule by an UPPER confidence bound on its loss instead of the loss itself:
#
#     score_i = mean_loss_i + beta * sqrt(2 * var_i * ln(2K/delta) / n_i) - prior_bonus
#
# which is pessimistic about noisy rules and optimistic about well-measured ones. The effect is that
# weight follows evidence, not one lucky print, which is the difference between an allocator that
# chases a spike and one that requires it to repeat.

def variance_aware(
    losses_by_expert: Mapping[str, Sequence[float]], *,
    delta: float = 0.1, beta: float = 1.0, alpha: float = 0.02,
    eta: float | None = None,
) -> dict[str, Any]:
    """Blend experts on a confidence bound over their loss, not on the raw loss."""
    names = tuple(losses_by_expert)
    if not names:
        raise ExpertsError("no_experts", "empty mapping")
    series = [list(losses_by_expert[name]) for name in names]
    lengths = {len(s) for s in series}
    if len(lengths) != 1:
        raise ExpertsError("ragged", f"expert series lengths {sorted(lengths)}")
    rounds = lengths.pop()
    if rounds == 0:
        raise ExpertsError("no_rounds", "every series is empty")
    if not (0.0 < delta < 1.0):
        raise ExpertsError("bad_delta", str(delta))
    if beta < 0:
        raise ExpertsError("bad_beta", str(beta))

    rate = eta if eta is not None else learning_rate(rounds, len(names))
    hedge = Hedge(n_experts=len(names), eta=rate, alpha=alpha)
    log_term = math.log(max(2.0, 2.0 * len(names) / delta))

    means = [0.0] * len(names)
    m2 = [0.0] * len(names)
    counts = [0] * len(names)
    rounds_out: list[Round] = []

    for index in range(rounds):
        scores: list[float] = []
        for i, series_i in enumerate(series):
            value = series_i[index]
            counts[i] += 1
            n = counts[i]
            delta_i = value - means[i]
            means[i] += delta_i / n
            m2[i] += delta_i * (value - means[i])
            var = (m2[i] / (n - 1)) if n > 1 else 0.0
            bonus = beta * math.sqrt(max(0.0, 2.0 * var * log_term / n)) if n > 1 else 0.0
            scores.append(means[i] + bonus)
        weights = hedge.update(scores)
        rounds_out.append(Round(index, tuple(scores), tuple(weights), hedge.leader()))

    cumulative = tuple(sum(s) for s in series)
    best = min(range(len(names)), key=lambda i: cumulative[i])
    blended = sum(
        sum(w * series[i][t] for i, w in enumerate(rnd.weights))
        for t, rnd in enumerate(rounds_out)
    )
    return {
        "names": list(names),
        "rounds": [r.to_dict() for r in rounds_out],
        "cumulative_loss": [round(x, 4) for x in cumulative],
        "confidence_scores": [round(m, 4) for m in means],
        "blended_cumulative_loss": round(blended, 4),
        "best_expert": names[best],
        "final_weights": list(rounds_out[-1].weights),
        "regret": round(blended - cumulative[best], 4),
        "caveats": [
            "scores are upper confidence bounds on loss, so a noisy rule is penalised until its "
            "evidence accumulates",
            "the bound uses empirical Bernstein scaling; it is not a proven regret bound for the "
            "blended sequence, only a principled weighting",
            "with fewer than two observations per rule the variance term is zero and the method "
            "degenerates to plain Hedge - by design",
        ],
    }


def weights_from_variance_aware(payload: Mapping[str, Any]) -> dict[str, float]:
    """The allocation a sizing layer reads, from the variance-aware blend."""
    return {
        name: float(weight)
        for name, weight in zip(payload["names"], payload["final_weights"], strict=False)
    }

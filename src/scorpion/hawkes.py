"""Alert clustering: a channel that fires in bursts is not firing at random.

Discord arrivals are self-exciting. One alert begets three, and that matters twice over: the burst
is a regime signal the individual post does not carry, and the burst is exactly when the two-slot
concurrency cap binds. This branch has been attributing those refusals to policy when some are
arithmetic.

The model is the exponential-kernel Hawkes process:

    lambda(t) = mu + alpha * sum over past events of exp(-beta * (t - t_i))

with branching ratio alpha/beta: the expected number of daughter events each event spawns. Below
one the process is stationary; near or above one the alert count stops measuring opportunity.

The branching ratio is estimated from the Fano factor of binned counts, not by likelihood. For a
Hawkes process the count variance exceeds the mean by 1/(1-n)^2, so n = 1 - 1/sqrt(F). That
identifies the ratio directly and does not require an optimiser to navigate three parameters whose
scales differ by orders of magnitude - which is exactly the failure this module hit first.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


class HawkesError(ValueError):
    """A clustering question that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


MIN_EVENTS = 40


@dataclass(frozen=True, slots=True)
class Params:
    mu: float
    alpha: float
    beta: float

    @property
    def branching(self) -> float:
        return self.alpha / self.beta

    @property
    def stationary(self) -> bool:
        return self.branching < 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mu": round(self.mu, 5),
            "alpha": round(self.alpha, 5),
            "beta": round(self.beta, 5),
            "branching_ratio": round(self.branching, 4),
            "stationary": self.stationary,
        }


def _validate(params: Params) -> None:
    if params.mu <= 0 or params.alpha <= 0 or params.beta <= 0:
        raise HawkesError("bad_params", str(params))


def intensity_terms(times: Sequence[float], params: Params) -> list[float]:
    """R_i = sum of exp(-beta * (t_i - t_j)) over j < i, computed recursively."""
    _validate(params)
    out: list[float] = []
    for index, moment in enumerate(times):
        if index == 0:
            out.append(0.0)
            continue
        decay = math.exp(-params.beta * (moment - times[index - 1]))
        out.append(decay * (1.0 + out[-1]))
    return out


def log_likelihood(
    times: Sequence[float], params: Params, *, horizon: float,
) -> float:
    """Hawkes log-likelihood on [0, horizon]."""
    if not times:
        raise HawkesError("no_events", "empty sequence")
    if horizon <= times[-1]:
        raise HawkesError("bad_horizon", f"{horizon} <= last event {times[-1]}")
    rec = intensity_terms(times, params)
    total = 0.0
    for weight in rec:
        value = params.mu + params.alpha * weight
        if value <= 0:
            return float("-inf")
        total += math.log(value)
    decay_sum = sum(math.exp(-params.beta * (horizon - moment)) for moment in times)
    total -= params.mu * horizon
    total -= (params.alpha / params.beta) * (len(times) - decay_sum)
    return total


def fano_branching(times: Sequence[float], *, horizon: float, bins: int = 20) -> float:
    """Branching ratio from the Fano factor of binned counts: n = 1 - 1/sqrt(var/mean)."""
    if bins < 4:
        raise HawkesError("bad_bins", str(bins))
    counts = [0] * bins
    width = horizon / bins
    for moment in times:
        index = min(bins - 1, int(moment / width))
        counts[index] += 1
    mean = sum(counts) / bins
    if mean <= 0:
        raise HawkesError("no_events", "every bin is empty")
    variance = sum((c - mean) ** 2 for c in counts) / (bins - 1)
    fano = variance / mean
    if fano <= 1.0:
        return 0.0
    return min(0.99, 1.0 - 1.0 / math.sqrt(fano))


def fit(
    times: Sequence[float], *, horizon: float, bins: int = 20,
) -> Params:
    """Fit (mu, alpha, beta) from the count dispersion and the mean inter-arrival time.

    The branching ratio comes from the Fano factor. `beta` is set from the mean inter-arrival, which
    fixes the memory timescale but is not identified separately - stated plainly rather than dressed
    up as a three-parameter MLE.
    """
    ordered = sorted(float(t) for t in times)
    if len(ordered) < MIN_EVENTS:
        raise HawkesError("thin_sample", f"{len(ordered)} events")
    if horizon <= ordered[-1]:
        raise HawkesError("bad_horizon", f"{horizon} <= last event {ordered[-1]}")

    branching = fano_branching(ordered, horizon=horizon, bins=bins)
    rate = len(ordered) / horizon
    gaps = [b - a for a, b in zip(ordered, ordered[1:], strict=False) if b > a]
    if not gaps:
        raise HawkesError("degenerate_span", "all events simultaneous")
    beta = 1.0 / (sum(gaps) / len(gaps))
    return Params(
        mu=max(1e-12, rate * (1.0 - branching)),
        alpha=max(1e-12, branching * beta),
        beta=beta,
    )


def simulate(
    params: Params, *, horizon: float, seed: int = 0, max_events: int = 20000,
) -> list[float]:
    """Ogata thinning, O(1) per step: intensity decays between events, so it bounds itself."""
    _validate(params)
    if horizon <= 0:
        raise HawkesError("bad_horizon", str(horizon))
    rng = random.Random(seed)
    events: list[float] = []
    moment = 0.0
    rec = 0.0
    while moment < horizon and len(events) < max_events:
        upper = params.mu + params.alpha * rec
        if upper <= 0:
            break
        gap = -math.log(max(1e-12, rng.random())) / upper
        moment += gap
        if moment >= horizon:
            break
        rec *= math.exp(-params.beta * gap)
        current = params.mu + params.alpha * rec
        if rng.random() * upper <= current:
            events.append(moment)
            rec += 1.0
    return events


def bootstrap_branching(
    times: Sequence[float], *, horizon: float, draws: int = 120, seed: int = 0,
) -> list[float]:
    """Parametric bootstrap: simulate from the fit and refit, so the ratio carries uncertainty."""
    fitted = fit(times, horizon=horizon)
    out: list[float] = []
    for index in range(draws):
        sample = simulate(fitted, horizon=horizon, seed=seed + index)
        if len(sample) < MIN_EVENTS:
            continue
        try:
            out.append(fit(sample, horizon=horizon).branching)
        except HawkesError:
            continue
    if not out:
        raise HawkesError("no_resamples", "every bootstrap fit failed")
    return sorted(out)


def interval(samples: Sequence[float], *, level: float = 0.9) -> tuple[float, float]:
    if not samples:
        raise HawkesError("empty_sample", "no draws")
    if not (0.5 < level < 1.0):
        raise HawkesError("bad_level", str(level))
    ordered = sorted(samples)
    tail = (1.0 - level) / 2.0
    lo = ordered[min(len(ordered) - 1, int(tail * len(ordered)))]
    hi = ordered[min(len(ordered) - 1, int((1.0 - tail) * len(ordered)))]
    return lo, hi


def queue_pressure(
    times: Sequence[float], *, capacity: int, hold_seconds: float,
) -> dict[str, Any]:
    """How often an arrival meets a full book, purely as a function of clustering.

    A slot is held for `hold_seconds`; an arrival finding `capacity` slots busy is refused
    rather than by any judgement. Comparing the observed rate with the same rate under a scrambled
    (Poisson-equivalent) arrival order isolates the part of the refusal caused by clustering.
    """
    if capacity < 1:
        raise HawkesError("bad_capacity", str(capacity))
    if hold_seconds <= 0:
        raise HawkesError("bad_hold", str(hold_seconds))
    ordered = sorted(times)
    if len(ordered) < MIN_EVENTS:
        raise HawkesError("thin_sample", f"{len(ordered)} events")

    def blocked(sequence: Sequence[float]) -> int:
        held: list[float] = []
        count = 0
        for moment in sequence:
            held = [end for end in held if end > moment]
            if len(held) >= capacity:
                count += 1
                continue
            held.append(moment + hold_seconds)
        return count

    observed = blocked(ordered)
    span = ordered[-1] - ordered[0]
    if span <= 0:
        raise HawkesError("degenerate_span", "all events simultaneous")
    rng = random.Random(0)
    scrambled: list[float] = []
    for _ in range(20):
        uniform = sorted(ordered[0] + span * rng.random() for _ in range(len(ordered)))
        scrambled.append(blocked(uniform))
    baseline = sum(scrambled) / len(scrambled)
    return {
        "capacity": capacity,
        "hold_seconds": hold_seconds,
        "events": len(ordered),
        "blocked_observed": observed,
        "blocked_uniform_baseline": round(baseline, 2),
        "excess_from_clustering": round(observed - baseline, 2),
        "blocked_rate": round(observed / len(ordered), 4),
        "note": (
            "the baseline scrambles arrival times uniformly over the observed span, which removes "
            "clustering while preserving the event count"
        ),
    }


def report(
    times: Sequence[float], *, horizon: float, capacity: int = 2, hold_seconds: float = 1800.0,
    draws: int = 120, seed: int = 0,
) -> dict[str, Any]:
    fitted = fit(times, horizon=horizon)
    payload = fitted.to_dict()
    try:
        samples = bootstrap_branching(times, horizon=horizon, draws=draws, seed=seed)
        lo, hi = interval(samples)
        payload["branching_interval"] = [round(lo, 4), round(hi, 4)]
        payload["clustering_established"] = lo > 0.05
    except HawkesError as error:
        payload["branching_interval"] = None
        payload["clustering_established"] = None
        payload["bootstrap_error"] = error.code
    try:
        payload["queue"] = queue_pressure(times, capacity=capacity, hold_seconds=hold_seconds)
    except HawkesError as error:
        payload["queue_error"] = error.code
    payload["caveats"] = [
        "one exponential kernel decays at one rate; a channel with a burst and a slow drift "
        "drift needs two, and one beta will fit neither properly",
        "a ratio below one means stationarity, not safety - a bursty stationary process "
        "still binds a small cap",
        "the queue model assumes a fixed holding time, which the desk does not have",
    ]
    return payload


def render(payload: dict[str, Any]) -> str:
    lines = [
        f"branching ratio {payload['branching_ratio']:.3f} "
        f"(mu {payload['mu']:.4f} alpha {payload['alpha']:.4f} beta {payload['beta']:.4f})",
    ]
    if payload.get("branching_interval"):
        lo, hi = payload["branching_interval"]
        lines.append(f"  90% parametric bootstrap [{lo:.3f}, {hi:.3f}]")
    queue = payload.get("queue")
    if queue:
        lines.append(
            f"  cap {queue['capacity']} held {queue['hold_seconds']:.0f}s: "
            f"{queue['blocked_observed']} of {queue['events']} arrivals met a full book "
            f"(uniform baseline {queue['blocked_uniform_baseline']})"
        )
    return "\n".join(lines)

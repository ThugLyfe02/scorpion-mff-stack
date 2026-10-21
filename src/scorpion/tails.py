"""Tail estimation: what the corpus has not sampled yet.

`ruin.py` reports the worst corpus trade at -0.39R and concludes there are no total losses: true
of a 107-trade sample, false of the instrument: a long 0DTE option's commonest outcome
is -1.00R, and seven months without one expiry at zero is evidence about the calendar, not the risk.

Pickands-Balkema-de Haan says exceedances over a high threshold converge to a generalised Pareto
distribution whatever the underlying process, so the tail can be ESTIMATED rather than extrapolated
from a maximum. The fit here is by method of moments, solved in closed form:

    k = 0.5 * (1 - mean^2 / variance),   alpha = mean * (1 - k)

Method of moments rather than maximum likelihood because with ~100 observations a likelihood fit can
walk to a degenerate corner, and rather than probability-weighted moments because the weighting
convention is easy to get wrong and impossible to notice - it was wrong here first time, returning a
shape of 2.98 on a sample generated with 0.30.

Every shape is reported with an interval: a tail point estimate from 107 observations has
more decimal places than information.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


class TailError(ValueError):
    """A tail question that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


MIN_EXCEEDANCES = 20


@dataclass(frozen=True, slots=True)
class Fit:
    shape: float
    scale: float
    threshold: float
    exceedances: int
    sample: int

    @property
    def finite_mean(self) -> bool:
        """The GPD mean exists only when shape is below one."""
        return self.shape < 1.0

    def quantile(self, tail_probability: float) -> float:
        """Loss exceeded with probability `tail_probability`, given the threshold was crossed."""
        if not (0.0 < tail_probability < 1.0):
            raise TailError("bad_probability", str(tail_probability))
        if abs(self.shape) < 1e-9:
            return float(self.threshold + self.scale * -math.log(tail_probability))
        return float(
            self.threshold
            + (self.scale / self.shape) * (tail_probability ** (-self.shape) - 1.0)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": round(self.shape, 4),
            "scale": round(self.scale, 4),
            "threshold": round(self.threshold, 4),
            "exceedances": self.exceedances,
            "sample": self.sample,
            "finite_mean": self.finite_mean,
        }


def losses_from_r(returns: Sequence[float]) -> list[float]:
    """Loss in R: a trade that returned -1.0R contributes 1.0 of loss. Sorted ascending by loss."""
    return sorted(-float(r) for r in returns)


def mean_residual_life(
    losses: Sequence[float], thresholds: Sequence[float],
) -> list[dict[str, Any]]:
    """Mean excess over each threshold. A GPD tail shows as a straight line in threshold."""
    out: list[dict[str, Any]] = []
    for threshold in thresholds:
        over = [value - threshold for value in losses if value > threshold]
        if not over:
            continue
        out.append({
            "threshold": round(threshold, 4),
            "exceedances": len(over),
            "mean_excess": round(sum(over) / len(over), 4),
        })
    return out


def plausible_thresholds(losses: Sequence[float], *, steps: int = 8) -> list[float]:
    """Candidate thresholds between the median and the second-highest loss."""
    if len(losses) < MIN_EXCEEDANCES:
        raise TailError("thin_sample", f"{len(losses)} losses")
    ordered = sorted(losses)
    low = ordered[len(ordered) // 2]
    high = ordered[-2] if len(ordered) > 2 else ordered[-1]
    if high <= low:
        raise TailError("degenerate_support", f"{low} to {high}")
    return [low + (high - low) * (i + 1) / (steps + 1) for i in range(steps)]


def choose_threshold(
    losses: Sequence[float], *, minimum_exceedances: int = MIN_EXCEEDANCES,
) -> float:
    """The lowest threshold leaving enough exceedances to fit."""
    for row in mean_residual_life(losses, plausible_thresholds(losses)):
        if row["exceedances"] >= minimum_exceedances:
            return float(row["threshold"])
    raise TailError("no_threshold", f"none leaves {minimum_exceedances} exceedances")


def fit(losses: Sequence[float], *, threshold: float | None = None) -> Fit:
    """Generalised Pareto fit by probability-weighted moments."""
    ordered = sorted(float(v) for v in losses)
    if len(ordered) < MIN_EXCEEDANCES:
        raise TailError("thin_sample", f"{len(ordered)} losses for a tail fit")
    cut = choose_threshold(ordered) if threshold is None else float(threshold)
    excesses = [value - cut for value in ordered if value > cut]
    if len(excesses) < MIN_EXCEEDANCES:
        raise TailError("thin_tail", f"{len(excesses)} exceedances at {cut}")
    if min(excesses) < 0:
        raise TailError("bad_threshold", f"{cut} exceeds a loss")

    n = len(excesses)
    mean = sum(excesses) / n
    variance = sum((value - mean) ** 2 for value in excesses) / (n - 1)
    if mean <= 0 or variance <= 0:
        raise TailError("degenerate_moments", f"mean={mean:.4f} var={variance:.4f}")
    shape = 0.5 * (1.0 - mean * mean / variance)
    scale = mean * (1.0 - shape)
    if shape >= 0.5:
        raise TailError(
            "infinite_variance",
            f"shape {shape:.3f} - the GPD variance does not exist, so moments cannot identify it",
        )
    if scale <= 0:
        raise TailError("bad_scale", str(scale))
    return Fit(
        shape=shape, scale=scale, threshold=cut, exceedances=n, sample=len(ordered)
    )


def bootstrap_shape(
    losses: Sequence[float], *, threshold: float | None = None, draws: int = 400, seed: int = 0,
) -> list[float]:
    """Resample and refit, so the shape carries the uncertainty it actually has."""
    ordered = sorted(float(v) for v in losses)
    cut = choose_threshold(ordered) if threshold is None else float(threshold)
    rng = random.Random(seed)
    shapes: list[float] = []
    for _ in range(draws):
        sample = [ordered[rng.randrange(len(ordered))] for _ in range(len(ordered))]
        try:
            shapes.append(fit(sample, threshold=cut).shape)
        except TailError:
            continue
    if not shapes:
        raise TailError("no_resamples", "every bootstrap fit was degenerate")
    return sorted(shapes)


def interval(samples: Sequence[float], *, level: float = 0.9) -> tuple[float, float]:
    if not samples:
        raise TailError("empty_sample", "no draws")
    if not (0.5 < level < 1.0):
        raise TailError("bad_level", str(level))
    ordered = sorted(samples)
    tail = (1.0 - level) / 2.0
    lo = ordered[min(len(ordered) - 1, int(tail * len(ordered)))]
    hi = ordered[min(len(ordered) - 1, int((1.0 - tail) * len(ordered)))]
    return lo, hi


def tail_beyond(fit_value: Fit, level: float) -> dict[str, Any]:
    """Probability that a loss exceeds an absolute level, given the fitted tail.

    The corpus boundary matters: if mass beyond -1.00R is not zero, every report
    claiming "no total losses" is describing the sample rather than the instrument.
    """
    if level <= fit_value.threshold:
        raise TailError("below_threshold", f"{level} is not in the tail")
    ratio = 1.0 + fit_value.shape * (level - fit_value.threshold) / fit_value.scale
    if ratio <= 0:
        # a negative shape with a bounded tail: the level is beyond the distribution's support
        return {"level": level, "probability": 0.0, "bounded_out": True,
                "support_max": round(
                    fit_value.threshold - fit_value.scale / fit_value.shape, 4
                )}
    probability = (fit_value.exceedances / fit_value.sample) * ratio ** (-1.0 / fit_value.shape)
    return {"level": level, "probability": probability, "bounded_out": False}


def report(
    returns: Sequence[float], *, threshold: float | None = None, draws: int = 400, seed: int = 0,
    levels: Sequence[float] = (1.0, 2.0, 5.0),
) -> dict[str, Any]:
    losses = losses_from_r(returns)
    fitted = fit(losses, threshold=threshold)
    payload = fitted.to_dict()
    try:
        shapes = bootstrap_shape(losses, threshold=fitted.threshold, draws=draws, seed=seed)
        lo, hi = interval(shapes)
        payload["shape_interval"] = [round(lo, 4), round(hi, 4)]
        payload["heavy_tail_established"] = lo > 0.0
    except TailError as error:
        payload["shape_interval"] = None
        payload["bootstrap_error"] = error.code
        payload["heavy_tail_established"] = None

    payload["beyond"] = []
    for level in levels:
        if level > fitted.threshold:
            payload["beyond"].append(tail_beyond(fitted, level))
    payload["empirical_max_loss"] = round(max(losses), 4)
    payload["implied_1_in_100"] = round(fitted.quantile(0.01), 4)
    payload["caveats"] = [
        "the shape is estimated from exceedances only; a low threshold biases it, "
        "which is why the mean-residual-life line should be inspected before the number is used",
        "a heavy-tail conclusion needs shape significantly above zero, reported as an interval",
        "the fit says nothing about the probability of a tail EVENT, only its size given that the "
        "threshold was crossed",
    ]
    return payload


def render(payload: dict[str, Any]) -> str:
    lines = [
        f"GPD over {payload['threshold']:.2f}R: shape {payload['shape']:+.3f} "
        f"scale {payload['scale']:.3f} on {payload['exceedances']} of {payload['sample']}",
        f"  empirical worst {payload['empirical_max_loss']:.2f}R   "
        f"fitted 1-in-100 {payload['implied_1_in_100']:.2f}R",
    ]
    if payload.get("shape_interval"):
        lo, hi = payload["shape_interval"]
        established = payload.get("heavy_tail_established")
        lines.append(
            f"  shape 90% interval [{lo:+.3f}, {hi:+.3f}]  heavy tail established: {established}"
        )
    for row in payload["beyond"]:
        lines.append(
            f"  P(loss > {row['level']:.2f}R) = {row['probability']:.4f}"
        )
    return "\n".join(lines)

"""Roughness: what memory length does volatility actually have?

Every volatility estimate in this branch inherits a memory assumption it never states: an EWMA
decay, a GARCH order, a rolling window. Gatheral, Jaisson and Rosenbaum put the Hurst exponent of
log-volatility near H = 0.1, far rougher than those models assume - so an estimate built on the
wrong memory length is stale in a direction nobody checked.

The estimator is the standard one. For a log-price series the q-th absolute moment of the increment
over lag k scales as k^(qH), so regressing log mean|r_k| on log k gives H as the slope.

What makes this usable rather than decorative is the refusal: with the bar coverage this desk holds,
H is usually not identified, and the honest output is `not_identified` with the reason rather than a
number that will be quoted later as if it had been measured.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


class RoughError(ValueError):
    """A roughness question that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Estimate:
    hurst: float
    intercept: float
    stderr: float
    lags: int
    points: int

    @property
    def identified(self) -> bool:
        """Inside (0, 0.5) and more than two standard errors from 0.5."""
        return 0.0 < self.hurst < 0.5 and (0.5 - self.hurst) > 2.0 * self.stderr

    def to_dict(self) -> dict[str, Any]:
        return {
            "hurst": round(self.hurst, 4),
            "stderr": round(self.stderr, 4),
            "intercept": round(self.intercept, 4),
            "lags": self.lags,
            "points": self.points,
            "identified": self.identified,
            "verdict": (
                "rough" if self.identified and self.hurst < 0.25
                else "identified" if self.identified
                else "not_identified"
            ),
        }


def log_prices(prices: Sequence[float]) -> list[float]:
    out: list[float] = []
    for value in prices:
        if value <= 0:
            raise RoughError("non_positive_price", str(value))
        out.append(math.log(value))
    if len(out) < 4:
        raise RoughError("thin_series", f"{len(out)} prices")
    return out


def lagged_moments(
    prices: Sequence[float], *, lags: int, q: float = 1.0,
) -> list[tuple[int, float, int]]:
    """(lag, mean |increment|^q, count) for lags 1..lags, on non-overlapping increments."""
    logs = log_prices(prices)
    if lags < 2:
        raise RoughError("bad_lags", str(lags))
    if q <= 0:
        raise RoughError("bad_q", str(q))
    out: list[tuple[int, float, int]] = []
    for lag in range(1, lags + 1):
        values = [
            abs(logs[i] - logs[i - lag]) ** q for i in range(lag, len(logs))
        ]
        if len(values) < 8:
            raise RoughError("thin_lag", f"lag {lag} has {len(values)} increments")
        out.append((lag, sum(values) / len(values), len(values)))
    return out


def _fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float, int]:
    """OLS slope, intercept and slope standard error."""
    n = len(xs)
    if n < 3:
        raise RoughError("thin_fit", f"{n} points")
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        raise RoughError("degenerate", "no variation in logs")
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=False)]
    if n > 2:
        variance = sum(r * r for r in residuals) / (n - 2)
        stderr = math.sqrt(variance / sxx) if variance > 0 else 0.0
    else:
        stderr = 0.0
    return slope, intercept, stderr, n


def estimate(prices: Sequence[float], *, lags: int = 5, q: float = 1.0) -> Estimate:
    """Hurst exponent of log-volatility from the moment-scaling regression."""
    moments = lagged_moments(prices, lags=lags, q=q)
    xs: list[float] = []
    ys: list[float] = []
    for lag, value, _count in moments:
        if value <= 0:
            raise RoughError("zero_moment", f"lag {lag}")
        xs.append(math.log(lag))
        ys.append(math.log(value))
    slope, intercept, stderr, points = _fit(xs, ys)
    return Estimate(
        hurst=slope / q, intercept=intercept, stderr=stderr / q, lags=lags, points=points
    )


def block_bootstrap(
    prices: Sequence[float], *, lags: int = 5, q: float = 1.0, block: int = 12,
    draws: int = 200, seed: int = 0,
) -> list[float]:
    """Resample contiguous blocks so the roughness inside each block survives the resampling."""
    if block < 2:
        raise RoughError("bad_block", str(block))
    if len(prices) < block * 3:
        raise RoughError("thin_series", f"{len(prices)} prices for blocks of {block}")
    rng = random.Random(seed)
    prices_list = list(prices)
    out: list[float] = []
    for _ in range(draws):
        sample: list[float] = []
        while len(sample) < len(prices_list):
            start = rng.randrange(0, len(prices_list) - block + 1)
            sample.extend(prices_list[start : start + block])
        try:
            out.append(estimate(sample[: len(prices_list)], lags=lags, q=q).hurst)
        except RoughError:
            continue
    if not out:
        raise RoughError("no_resamples", "every resample was degenerate")
    return sorted(out)


def interval(samples: Sequence[float], *, level: float = 0.9) -> tuple[float, float]:
    if not samples:
        raise RoughError("empty_sample", "no bootstrap draws")
    if not (0.5 < level < 1.0):
        raise RoughError("bad_level", str(level))
    tail = (1.0 - level) / 2.0
    ordered = sorted(samples)
    lo = ordered[min(len(ordered) - 1, int(tail * len(ordered)))]
    hi = ordered[min(len(ordered) - 1, int((1.0 - tail) * len(ordered)))]
    return lo, hi


def vol_memory(estimate_value: Estimate, *, max_bars: int) -> dict[str, Any]:
    """The effective memory length implied by H, capped at what the data can support.

    For fBm the autocovariance of log-vol at lag k decays like k^(2H-2), so memory is short
    is small. The half-life of that decay is the number a rolling window should be built around.
    """
    if max_bars < 2:
        raise RoughError("bad_cap", str(max_bars))
    if not estimate_value.identified:
        return {
            "bars": max_bars,
            "identified": False,
            "note": (
                "H is not identified on this sample, so the window stays at the data-supported "
                "maximum rather than at a number the estimate cannot justify"
            ),
        }
    exponent = 2.0 * estimate_value.hurst - 2.0
    if exponent >= 0:
        raise RoughError("degenerate_exponent", str(exponent))
    half_life = max(1, int(round(math.log(0.5) / exponent)))
    return {
        "bars": min(half_life, max_bars),
        "identified": True,
        "note": f"hu{estimate_value.hurst:.3f} implies a {half_life}-bar autocovariance half-life",
    }


def report(
    prices: Sequence[float], *, lags: int = 5, block: int = 12, draws: int = 200, seed: int = 0,
) -> dict[str, Any]:
    est = estimate(prices, lags=lags)
    payload = est.to_dict()
    try:
        samples = block_bootstrap(
            prices, lags=lags, block=block, draws=draws, seed=seed
        )
        lo, hi = interval(samples)
        payload["interval"] = [round(lo, 4), round(hi, 4)]
        payload["crosses_half"] = lo <= 0.5 <= hi
    except RoughError as error:
        payload["interval"] = None
        payload["bootstrap_error"] = error.code
    payload["memory"] = vol_memory(est, max_bars=max(2, len(prices) // 4))
    payload["caveats"] = [
        "the estimator assumes moment scaling is linear in log lag; on a short sample it "
        "and the residuals should be inspected before the slope is believed",
        "a block bootstrap preserves roughness within blocks but not across them, so the "
        "a lower bound on uncertainty",
        "H near 0.5 is indistinguishable from a random walk with this sample size, and is reported "
        "as not_identified rather than as a finding",
    ]
    return payload


def render(payload: dict[str, Any]) -> str:
    line = f"H = {payload['hurst']:.3f} +/- {payload['stderr']:.3f}  ({payload['verdict']})"
    if payload.get("interval"):
        lo, hi = payload["interval"]
        line += f"  90% block bootstrap [{lo:.3f}, {hi:.3f}]"
    return f"{line}\n  memory: {payload['memory']['bars']} bars - {payload['memory']['note']}"

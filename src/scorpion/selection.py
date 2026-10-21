"""Selection: what to pay, and which strike to buy, from measured distributions.

Two decisions happen before any order exists, and both have been made by intuition: how much
premium is acceptable for this signal, and which strike to take.

* expected R conditional on premium. Cheap and expensive contracts do not share a return
  distribution, and the corpus can say so. Bands are formed on entry premium, R is measured inside
  each band, and a band is withheld below the floor like every other statistic here.
* breakeven premium: the top of the highest band whose expected R is still positive. Above it a
  trade is a donation regardless of the signal, and the number is walked out of the data rather
  than asserted.
* strike choice from a target delta, by inverting the same logistic that `exposure.delta_proxy`
  uses forward - one definition of delta in the system, used in both directions, so a cap and a
  selector cannot disagree.

The honest limits, stated in the report:

* premium is a proxy for moneyness and liquidity; spreads and open interest are not recorded;
* expected R inherits every caveat in `ruin.py` - claimed exits, no fees, survivorship;
* a strike chosen from a target delta prices the risk you asked for, which is not the same as a
  strike that makes money.

No optimisation over the corpus is performed. Fitting a band to maximise historical R would produce
a number that describes this file and nothing else; the bands are fixed buckets, and the table says
which of them carry evidence.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

D_MIN_BAND_SAMPLE = 5


class SelectionError(ValueError):
    """A selection request that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Band:
    """A premium bucket, half-open: [low, high)."""

    low: float
    high: float
    label: str = ""

    def __post_init__(self) -> None:
        if self.low < 0 or self.high <= self.low:
            raise SelectionError("bad_band", f"[{self.low}, {self.high})")

    def contains(self, premium: float) -> bool:
        return self.low <= premium < self.high

    def to_dict(self) -> dict[str, Any]:
        return {"low": self.low, "high": self.high,
                "label": self.label or f"{self.low:g}-{self.high:g}"}


DEFAULT_BANDS: tuple[Band, ...] = (
    Band(0.00, 0.50, "penny"),
    Band(0.50, 1.00, "cheap"),
    Band(1.00, 2.00, "mid"),
    Band(2.00, 4.00, "rich"),
    Band(4.00, 1_000.00, "premium"),
)


@dataclass(frozen=True, slots=True)
class BandStats:
    band: Band
    n: int
    mean_r: float
    median_r: float
    win_rate: float
    stdev_r: float
    total_r: float
    withheld: bool = False

    @property
    def positive(self) -> bool:
        return not self.withheld and self.n >= D_MIN_BAND_SAMPLE and self.mean_r > 0

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "band": self.band.to_dict(), "n": self.n, "withheld": self.withheld,
        }
        if not self.withheld:
            payload.update({
                "mean_r": round(self.mean_r, 4),
                "median_r": round(self.median_r, 4),
                "win_rate": round(self.win_rate, 2),
                "stdev_r": round(self.stdev_r, 4),
                "total_r": round(self.total_r, 2),
            })
        return payload


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(sum((v - average) ** 2 for v in values) / (len(values) - 1))


def band_stats(
    pairs: Iterable[tuple[float, float]], band: Band, *, floor: int = D_MIN_BAND_SAMPLE,
) -> BandStats:
    """R inside one premium band, withheld when the sample is too thin to mean anything."""
    values = [r for premium, r in pairs if band.contains(premium)]
    if len(values) < floor:
        return BandStats(band, len(values), 0.0, 0.0, 0.0, 0.0, 0.0, withheld=True)
    wins = [v for v in values if v > 0]
    return BandStats(
        band=band, n=len(values), mean_r=_mean(values), median_r=_median(values),
        win_rate=100.0 * len(wins) / len(values), stdev_r=_stdev(values),
        total_r=sum(values),
    )


def premium_pairs(ledger: Any) -> list[tuple[float, float]]:
    """(entry premium, realised R) for every trade that has both."""
    out: list[tuple[float, float]] = []
    for trade in ledger.trades:
        r_multiple = trade.r_multiple
        if r_multiple is None:
            continue
        out.append((float(trade.entry_bid), float(r_multiple)))
    return out


def table(
    ledger: Any, bands: Sequence[Band] = DEFAULT_BANDS, *, floor: int = D_MIN_BAND_SAMPLE,
) -> dict[str, Any]:
    pairs = premium_pairs(ledger)
    rows = [band_stats(pairs, band, floor=floor) for band in bands]
    usable = [row for row in rows if not row.withheld]
    best = max(usable, key=lambda row: row.mean_r) if usable else None
    return {
        "trades": len(pairs),
        "bands": [row.to_dict() for row in rows],
        "best_band": best.band.to_dict() if best is not None else None,
        "breakeven_premium": breakeven_premium(rows),
        "floor": floor,
        "caveats": [
            "premium is a proxy for moneyness and liquidity; spreads and open interest are not "
            "recorded",
            "expected R inherits the ruin model's caveats: claimed exits, no fees, survivorship",
            "bands are fixed buckets, not fitted - a fitted band would describe this corpus only",
        ],
    }


def breakeven_premium(rows: Sequence[BandStats]) -> float | None:
    """The top of the highest band whose expected R is still positive.

    Walked in band order and stopped at the first non-positive one, so the answer is a
    boundary the data supports rather than an interpolation between two means.
    """
    usable = [row for row in rows if not row.withheld]
    if not usable:
        return None
    boundary: float | None = None
    for row in sorted(usable, key=lambda r: r.band.low):
        if row.mean_r <= 0:
            break
        boundary = row.band.high
    return boundary


def expected_r(ledger: Any, premium: float, bands: Sequence[Band] = DEFAULT_BANDS) -> float | None:
    """Expected R for a premium, from the band it falls in. None when that band is withheld."""
    for band in bands:
        if band.contains(premium):
            stats = band_stats(premium_pairs(ledger), band)
            return None if stats.withheld else stats.mean_r
    return None


def gate(
    ledger: Any, premium: float, *, bands: Sequence[Band] = DEFAULT_BANDS,
    minimum_r: float = 0.0,
) -> dict[str, Any]:
    """Accept or refuse a premium, with the reason and the number behind it."""
    value = expected_r(ledger, premium, bands)
    if value is None:
        return {"accept": False, "reason": "no_evidence_for_band", "premium": premium,
                "expected_r": None}
    if value <= minimum_r:
        return {"accept": False, "reason": "negative_expectation", "premium": premium,
                "expected_r": round(value, 4)}
    return {"accept": True, "reason": "", "premium": premium,
            "expected_r": round(value, 4)}


# ── strike choice from a target delta ──────────────────────────────────────
def _logit(p: float) -> float:
    if not 0.0 < p < 1.0:
        raise SelectionError("bad_delta", str(p))
    return math.log(p / (1.0 - p))


def strike_for_delta(
    spot: float, *, side: str, target_delta: float, expiry: date, as_of: date,
) -> float:
    """Invert the delta proxy: choose the delta, solve for the strike.

    The forward model (see `exposure.delta_proxy`) is a logistic in log-moneyness with scale
    0.5*sqrt(T). For a call, delta = logistic(z); for a put, |delta| = 1 - logistic(z). Both invert
    exactly, so a cap and a selector cannot disagree about what delta a strike carries.
    """
    if spot <= 0:
        raise SelectionError("bad_spot", str(spot))
    if side not in ("CALL", "PUT"):
        raise SelectionError("bad_side", side)
    magnitude = abs(target_delta)
    if not 0.0 < magnitude < 1.0:
        raise SelectionError("bad_delta", str(target_delta))

    days = max(1, (expiry - as_of).days)
    scale = max(0.03, 0.5 * math.sqrt(days / 365.0))

    call_probability = magnitude if side == "CALL" else 1.0 - magnitude
    z = _logit(call_probability)
    moneyness = z * scale
    return spot / math.exp(moneyness)


def strike_band(
    spot: float, *, side: str, expiry: date, as_of: date,
    targets: Sequence[float] = (0.3, 0.5, 0.7),
) -> list[dict[str, Any]]:
    """The strikes carrying a range of deltas, so a choice can be made rather than assumed."""
    out: list[dict[str, Any]] = []
    for target in targets:
        strike = strike_for_delta(spot, side=side, target_delta=target, expiry=expiry, as_of=as_of)
        out.append({
            "target_delta": target,
            "strike": round(strike, 2),
            "distance_pct": round(100.0 * (strike / spot - 1.0), 2),
        })
    return out


def nearest_trade_strike(candidates: Sequence[float], strike: float) -> float | None:
    """The closest listed strike, because an option only exists at a strike that exists."""
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: abs(candidate - strike))


def describe(payload: Mapping[str, Any]) -> str:
    lines = [f"{payload['trades']} trades priced across {len(payload['bands'])} bands"]
    for row in payload["bands"]:
        band = row["band"]["label"]
        if row["withheld"]:
            lines.append(f"  {band:10s} n={row['n']:3d}  withheld")
        else:
            lines.append(
                f"  {band:10s} n={row['n']:3d}  mean {row['mean_r']:+.3f}R  "
                f"win {row['win_rate']:5.1f}%  sd {row['stdev_r']:.2f}"
            )
    breakeven = payload["breakeven_premium"]
    if breakeven is not None:
        lines.append(f"  breakeven premium: {breakeven:g}")
    return "\n".join(lines)


def summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    usable = [row for row in payload["bands"] if not row["withheld"]]
    return {
        "trades": payload["trades"],
        "bands_with_evidence": len(usable),
        "bands_withheld": len(payload["bands"]) - len(usable),
        "breakeven_premium": payload["breakeven_premium"],
        "best_band": payload["best_band"],
    }

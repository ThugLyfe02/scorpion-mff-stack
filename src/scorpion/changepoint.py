"""Change-point detection: alarm when the process changes, not when the session ends.

`regime.py` labels a session after the fact, which helps research and does nothing for a decision.
The question a desk asks mid-session is different: has the thing I am trading stopped behaving like
the thing I measured? That is sequential detection, and the textbook answer is CUSUM.

Two-sided CUSUM accumulates deviations from a target in both directions and alarms when either
accumulator crosses a boundary:

    s_up = max(0, s_up + (x - target - k))
    s_dn = max(0, s_dn - (x - target + k))
    alarm when s_up > h or s_dn > h, then reset

`k` is the shift worth detecting and `h` the boundary, both in units of the series' own scale, so
the same configuration means the same thing on a $0.50 contract and a $50 one.

Three design choices that matter:

* no lookahead: the statistic at bar i uses bars up to i only, so an alarm time is a time a real
  system could have acted at;
* alarm, then re-arm: without a reset the detector stays latched above the boundary and fires on
  every later bar, with one a sustained change reports periodically instead of continuously;
* a measured false-alarm rate: `average_run_length` simulates a stationary series and reports how
  often the configuration cries wolf, because a detector whose error rate is asserted rather than
  measured is a random number generator with a threshold attached.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any


class ChangePointError(ValueError):
    """A detection request that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Config:
    """Detection parameters, in units of the series' own standard deviation."""

    shift: float = 0.5          # k: the shift worth detecting
    threshold: float = 5.0      # h: boundary, in sigma of the accumulation
    target: float = 0.0         # mu_0: the in-control mean
    reset_after_alarm: bool = True

    def __post_init__(self) -> None:
        if self.shift <= 0:
            raise ChangePointError("bad_shift", f"{self.shift}")
        if self.threshold <= 0:
            raise ChangePointError("bad_threshold", f"{self.threshold}")

    def to_dict(self) -> dict[str, float]:
        return {"shift": self.shift, "threshold": self.threshold, "target": self.target}


@dataclass(frozen=True, slots=True)
class Alarm:
    index: int
    direction: str              # up | down
    statistic: float
    value: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "direction": self.direction,
            "statistic": round(self.statistic, 3), "value": round(self.value, 6),
        }


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def standardise(series: Sequence[float]) -> list[float]:
    """Centre and scale by the series' own deviation, so k and h are comparable across contracts."""
    if not series:
        return []
    mean = sum(series) / len(series)
    sigma = _stdev(series) or 1.0
    return [(value - mean) / sigma for value in series]


def detect(
    series: Sequence[float], config: Config | None = None, *, standardise_input: bool = False,
) -> list[Alarm]:
    """Two-sided CUSUM. Returns every alarm in order, with the bar it fired on."""
    config = config or Config()
    values = standardise(series) if standardise_input else list(series)
    if not values:
        return []

    up = 0.0
    down = 0.0
    alarms: list[Alarm] = []
    for index, value in enumerate(values):
        deviation = value - config.target
        up = max(0.0, up + deviation - config.shift)
        down = max(0.0, down - deviation - config.shift)

        fired: Alarm | None = None
        if up > config.threshold:
            fired = Alarm(index, "up", up, value)
        elif down > config.threshold:
            fired = Alarm(index, "down", down, value)

        if fired is not None:
            alarms.append(fired)
            if config.reset_after_alarm:
                up = 0.0
                down = 0.0
    return alarms


def detect_bars(barset: Any, config: Config | None = None) -> list[Alarm]:
    """CUSUM on log returns of a bar set, which is the series a desk actually watches."""
    closes = [float(bar.close) for bar in barset.bars]
    if len(closes) < 3:
        return []
    if any(c <= 0 for c in closes):
        raise ChangePointError("non_positive_close", barset.contract_key)
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    return detect(returns, config, standardise_input=True)


def average_run_length(
    config: Config | None = None, *, runs: int = 200, bars: int = 500, seed: int = 7,
) -> dict[str, Any]:
    """Simulated in-control run length: how long a stationary series goes before a false alarm.

    Reported rather than asserted. A detector whose error rate is a matter of opinion is a threshold
    with a story attached.
    """
    config = config or Config()
    rng = random.Random(seed)
    lengths: list[int] = []
    for _ in range(runs):
        series = [rng.gauss(0.0, 1.0) for _ in range(bars)]
        alarms = detect(series, config)
        lengths.append(alarms[0].index if alarms else bars)

    lengths.sort()
    return {
        "config": config.to_dict(),
        "runs": runs, "bars": bars, "seed": seed,
        "median_run": lengths[len(lengths) // 2],
        "p10_run": lengths[int(0.1 * (len(lengths) - 1))],
        "alarmed_within_window_pct": round(100.0 * sum(1 for x in lengths if x < bars) / runs, 1),
    }


def detection_latency(
    config: Config | None = None, *, shift: float = 2.0, runs: int = 100, bars: int = 200,
    change_at: int = 100, seed: int = 11,
) -> dict[str, Any]:
    """Bars between a planted change and the alarm. The number that makes a detector useful."""
    config = config or Config()
    rng = random.Random(seed)
    latencies: list[int] = []
    missed = 0
    for _ in range(runs):
        series = [rng.gauss(0.0, 1.0) for _ in range(change_at)]
        series += [rng.gauss(shift, 1.0) for _ in range(bars - change_at)]
        alarms = detect(series, config)
        after = [a for a in alarms if a.index >= change_at]
        if not after:
            missed += 1
        else:
            latencies.append(after[0].index - change_at)

    latencies.sort()
    return {
        "planted_shift": shift,
        "detected_pct": round(100.0 * (runs - missed) / runs, 1),
        "median_latency_bars": latencies[len(latencies) // 2] if latencies else None,
        "p90_latency_bars": latencies[int(0.9 * (len(latencies) - 1))] if latencies else None,
        "missed": missed,
        "runs": runs,
    }


def report(series: Sequence[float], config: Config | None = None) -> dict[str, Any]:
    config = config or Config()
    alarms = detect(series, config, standardise_input=True)
    return {
        "bars": len(series),
        "config": config.to_dict(),
        "alarms": [a.to_dict() for a in alarms],
        "first_alarm_bar": alarms[0].index if alarms else None,
        "alarm_rate_pct": round(100.0 * len(alarms) / len(series), 2) if series else 0.0,
        "caveats": [
            "an alarm says the process changed, not why - the cause is a separate question",
            "a reset after each alarm means one sustained change reports once, not continuously",
        ],
    }


def render(payload: dict[str, Any]) -> str:
    lines = [
        f"{payload['bars']} bars  {len(payload['alarms'])} alarms  "
        f"rate {payload['alarm_rate_pct']}%"
    ]
    for alarm in payload["alarms"][:8]:
        lines.append(
            f"  bar {alarm['index']:4d}  {alarm['direction']:4s}  "
            f"stat {alarm['statistic']:.2f}"
        )
    return chr(10).join(lines)


def scan(barsets: Iterable[Any], config: Config | None = None) -> dict[str, Any]:
    """Every bar set scanned, with the ones that cannot be scanned named rather than skipped."""
    config = config or Config()
    rows: dict[str, Any] = {}
    for barset in barsets:
        try:
            alarms = detect_bars(barset, config)
        except ChangePointError as exc:
            rows[str(getattr(barset, "contract_key", "?"))] = {
                "alarms": 0, "error": f"{exc.code}: {exc.detail}",
            }
            continue
        rows[str(getattr(barset, "contract_key", "?"))] = {
            "bars": len(getattr(barset, "bars", ())),
            "alarms": len(alarms),
            "first_alarm_bar": alarms[0].index if alarms else None,
        }
    return {"config": config.to_dict(), "sets": rows}

"""Risk of ruin: how much of the claimed edge can be wrong before the desk stops working.

The corpus says mean +0.97R per trade at an 88% win rate. That number is not a market fact:
it is the trader's own record of exits he described, with no fills, no fees, no slippage and
no accounting for the alerts he chose to post. A sizing decision built on it directly is a
decision built on optimism.

So this module does not ask "how much would we have made". It asks the only question that
survives contact with a real venue:

    at what HAIRCUT to the claimed returns does the strategy stop surviving?

The answer - the breakeven haircut - is the honest strength of the edge. If claimed +0.97R
still clears the drawdown rule when cut in half, the sizing is defensible. If it needs 90% of
the claim, the edge is a story.

Mechanics, all deterministic given a seed:

* BOOTSTRAP the R-multiples with replacement. Common random numbers are used across every
  scenario (the same draw indices regardless of haircut or size), because otherwise the
  comparison between two haircuts is partly the comparison between two random draws - and the
  breakeven number would be noise rather than a threshold.
* COMPOUND equity trade by trade: equity *= 1 + f*R, where f is the fraction of equity risked
  per trade and R is clipped at -1 (a long option cannot lose more than its premium).
* HALT on peak-to-trough drawdown (the same rule the governor enforces), and count the run as
  halted from that point: a halted desk makes no further money, and pretending it would is how
  drawdown limits get quietly ignored.
* KELLY by grid, not by formula. The closed form assumes a two-outcome bet; the empirical
  distribution is not that, so the growth-optimal fraction is found by maximising the mean log
  growth over a grid - and then divided, because full Kelly on an estimated distribution is a
  known way to lose money on a real one.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

R_FLOOR = -1.0          # a long option's worst case is the premium
DEFAULT_GRID = tuple(round(0.0025 * i, 4) for i in range(1, 41))     # 0.25% .. 10%


@dataclass(frozen=True, slots=True)
class RuinConfig:
    start_equity: float = 100_000.0
    risk_fraction: float = 0.01        # of equity, per trade
    halt_drawdown: float = 0.20        # peak-to-trough; mirrors the governor's limit
    ruin_floor: float = 0.50           # below half the start is treated as ruin
    trades_per_run: int = 250
    runs: int = 2_000
    seed: int = 7
    haircut: float = 0.0               # multiply every R by (1 - haircut)

    def __post_init__(self) -> None:
        if not (0.0 < self.risk_fraction <= 0.25):
            raise ValueError("risk_fraction must be in (0, 0.25]")
        if not (0.0 < self.halt_drawdown < 1.0):
            raise ValueError("halt_drawdown must be in (0, 1)")
        if not (0.0 <= self.haircut < 1.0):
            raise ValueError("haircut must be in [0, 1)")
        if self.trades_per_run < 1 or self.runs < 1:
            raise ValueError("trades_per_run and runs must be positive")

    def fingerprint(self) -> str:
        payload = (f"{self.start_equity}|{self.risk_fraction}|{self.halt_drawdown}|"
                   f"{self.ruin_floor}|{self.trades_per_run}|{self.runs}|{self.seed}|"
                   f"{self.haircut}")
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class RunResult:
    terminal_equity: float
    max_drawdown: float
    halted: bool
    ruined: bool
    trades_taken: int


@dataclass(frozen=True, slots=True)
class Simulation:
    config: RuinConfig
    runs: tuple[RunResult, ...]

    @property
    def n(self) -> int:
        return len(self.runs)

    def _pct(self, values: Sequence[float], p: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        position = p * (len(ordered) - 1)
        low = math.floor(position)
        high = math.ceil(position)
        if low == high:
            return ordered[low]
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    def summary(self) -> dict[str, Any]:
        terminal = [r.terminal_equity for r in self.runs]
        drawdowns = [r.max_drawdown for r in self.runs]
        growth = [r.terminal_equity / self.config.start_equity for r in self.runs]
        halted = sum(1 for r in self.runs if r.halted)
        ruined = sum(1 for r in self.runs if r.ruined)
        # The drawdown rule only fires on long losing streaks; at desk-sized clips it often
        # cannot fire at all. "Ending the year down" is the exposure that remains, and it is
        # the number that separates a real edge from a claimed one.
        down = sum(1 for r in self.runs if r.terminal_equity < self.config.start_equity)
        return {
            "config": {
                "risk_fraction": self.config.risk_fraction,
                "haircut": self.config.haircut,
                "halt_drawdown": self.config.halt_drawdown,
                "trades_per_run": self.config.trades_per_run,
                "runs": self.config.runs,
                "seed": self.config.seed,
                "fingerprint": self.config.fingerprint(),
            },
            "p_halt": round(100.0 * halted / self.n, 2),
            "p_ruin": round(100.0 * ruined / self.n, 2),
            "p_down": round(100.0 * down / self.n, 2),
            "median_growth": round(self._pct(growth, 0.5), 3),
            "p05_growth": round(self._pct(growth, 0.05), 3),
            "p95_growth": round(self._pct(growth, 0.95), 3),
            "median_max_drawdown_pct": round(100 * self._pct(drawdowns, 0.5), 2),
            "p95_max_drawdown_pct": round(100 * self._pct(drawdowns, 0.95), 2),
            "median_terminal_equity": round(self._pct(terminal, 0.5), 2),
        }


def _draw_indices(runs: int, trades: int, size: int, seed: int) -> list[list[int]]:
    """Common random numbers: identical draws for every scenario compared against each other."""
    rng = random.Random(seed)
    return [[rng.randrange(size) for _ in range(trades)] for _ in range(runs)]


def simulate(rs: Sequence[float], config: RuinConfig) -> Simulation:
    if not rs:
        raise ValueError("no R-multiples to simulate")
    draws = _draw_indices(config.runs, config.trades_per_run, len(rs), config.seed)
    adjusted = [max(R_FLOOR, float(r)) * (1.0 - config.haircut) for r in rs]

    results: list[RunResult] = []
    for path in draws:
        equity = config.start_equity
        peak = equity
        worst = 0.0
        halted = False
        ruined = False
        taken = 0

        for index in path:
            if halted or ruined:
                break
            r = adjusted[index]
            stake = equity * config.risk_fraction
            equity += stake * r
            taken += 1
            peak = max(peak, equity)
            drawdown = 1.0 - (equity / peak) if peak > 0 else 1.0
            worst = max(worst, drawdown)
            if equity <= config.start_equity * config.ruin_floor or equity <= 0:
                ruined = True
            elif drawdown >= config.halt_drawdown:
                halted = True

        results.append(RunResult(
            terminal_equity=equity, max_drawdown=worst, halted=halted, ruined=ruined,
            trades_taken=taken,
        ))
    return Simulation(config=config, runs=tuple(results))


def growth_curve(rs: Sequence[float], fractions: Iterable[float]) -> list[dict[str, float]]:
    """Mean log growth per trade for each risk fraction: the Kelly objective, empirically."""
    values = [max(R_FLOOR, float(r)) for r in rs]
    out: list[dict[str, float]] = []
    for f in fractions:
        total = 0.0
        for r in values:
            term = 1.0 + f * r
            total += math.log(term) if term > 0 else -50.0
        out.append({"fraction": round(f, 4), "mean_log_growth": round(total / len(values), 6)})
    return out


def kelly(rs: Sequence[float], *, grid: Sequence[float] = DEFAULT_GRID) -> dict[str, Any]:
    """Growth-optimal fraction by grid search, plus the fractional-Kelly table."""
    curve = growth_curve(rs, grid)
    best = max(curve, key=lambda row: row["mean_log_growth"])
    optimal = float(best["fraction"])
    table = []
    for divisor in (1, 2, 4, 8):
        target = round(optimal / divisor, 4)
        match = min(curve, key=lambda row: abs(row["fraction"] - target))
        table.append({
            "kelly_divisor": divisor,
            "fraction": match["fraction"],
            "mean_log_growth": match["mean_log_growth"],
        })
    return {"optimal_fraction": optimal,
            "mean_log_growth_at_optimal": best["mean_log_growth"],
            "fractional": table}


def sensitivity(
    rs: Sequence[float], config: RuinConfig, *,
    fractions: Sequence[float] = (0.0025, 0.005, 0.01, 0.02, 0.03, 0.05),
    haircuts: Sequence[float] = (0.0, 0.25, 0.50, 0.75),
) -> list[dict[str, Any]]:
    """Risk fraction x haircut. The knee is where P(halt) stops being small."""
    rows: list[dict[str, Any]] = []
    for haircut in haircuts:
        for fraction in fractions:
            cfg = RuinConfig(
                start_equity=config.start_equity, risk_fraction=fraction,
                halt_drawdown=config.halt_drawdown, ruin_floor=config.ruin_floor,
                trades_per_run=config.trades_per_run, runs=config.runs, seed=config.seed,
                haircut=haircut,
            )
            summary = simulate(rs, cfg).summary()
            rows.append({
                "haircut": haircut,
                "risk_fraction": fraction,
                "p_halt": summary["p_halt"],
                "p_ruin": summary["p_ruin"],
                "median_growth": summary["median_growth"],
                "p05_growth": summary["p05_growth"],
                "median_max_drawdown_pct": summary["median_max_drawdown_pct"],
            })
    return rows


def breakeven_haircut(
    rs: Sequence[float], config: RuinConfig, *,
    max_p_halt: float = 5.0, tolerance: float = 0.01,
) -> dict[str, Any]:
    """The largest haircut at which P(halt) stays at or below `max_p_halt`.

    Found by bisection over the same common random draws, so the answer is a threshold rather
    than a difference between two unrelated samples. If even an unhaircut simulation breaches
    the bound, the breakeven is 0.0 and the edge cannot carry the size at all.
    """
    def p_halt_at(haircut: float) -> float:
        cfg = RuinConfig(
            start_equity=config.start_equity, risk_fraction=config.risk_fraction,
            halt_drawdown=config.halt_drawdown, ruin_floor=config.ruin_floor,
            trades_per_run=config.trades_per_run, runs=config.runs, seed=config.seed,
            haircut=haircut,
        )
        return float(simulate(rs, cfg).summary()["p_halt"])

    baseline = p_halt_at(0.0)
    if baseline > max_p_halt:
        return {"breakeven_haircut": 0.0, "p_halt_at_zero": baseline,
                "note": "even the unhaircut claim breaches the halt bound at this size"}

    low, high = 0.0, 0.99
    while high - low > tolerance:
        mid = (low + high) / 2
        if p_halt_at(mid) <= max_p_halt:
            low = mid
        else:
            high = mid
    return {
        "breakeven_haircut": round(low, 4),
        "p_halt_at_breakeven": p_halt_at(low),
        "p_halt_at_zero": baseline,
        "bound": max_p_halt,
    }


# ── the stress that actually bites ──────────────────────────────────────────
# A uniform haircut scales the mean while preserving the SHAPE of the distribution, and the
# shape is where the risk lives. The corpus records 8 losses in 101 trades and 6 "unknown"
# outcomes - a real desk with real fills sees a loss rate several times that, because the
# trades that quietly go to zero are exactly the ones nobody posts about. So the stress below
# reclassifies wins as losses until the loss rate reaches a realistic level, drawing the
# magnitude from the observed loss distribution. That changes the shape, not the mean.

def reclassify_losses(
    rs: Sequence[float], *, loss_rate: float, seed: int = 11,
    total_loss_share: float = 0.6, total_loss_r: float = -1.0,
) -> list[float]:
    """Rewrite the distribution to a target loss rate with realistic magnitudes.

    The corpus has NO total-loss trades - its worst is -0.39R - because a long option that
    expires worthless is not something anyone posts about. Recycling only the observed loss
    magnitudes therefore produces a distribution that cannot lose materially, and every
    scenario built on it looks safe. So a share of the stressed losses are TOTAL losses at
    -1.0R: the dominant real-world outcome for a 0DTE long option, and the one the corpus is
    silent about.
    """
    values = [max(R_FLOOR, float(r)) for r in rs]
    losses = [r for r in values if r < 0]
    wins = [r for r in values if r >= 0]
    if not (0.0 <= loss_rate < 1.0):
        raise ValueError("loss_rate must be in [0, 1)")
    if not (0.0 <= total_loss_share <= 1.0):
        raise ValueError("total_loss_share must be in [0, 1]")
    if not wins:
        return list(values)

    observed_loss = (sum(losses) / len(losses)) if losses else -0.5
    target_losses = int(round(loss_rate * len(values)))
    to_convert = max(0, min(len(wins), target_losses - len(losses)))
    rng = random.Random(seed)
    order = sorted(range(len(wins)), key=lambda i: wins[i])          # smallest wins first
    chosen = set(order[:to_convert])

    stressed: list[float] = list(losses)
    for index, value in enumerate(wins):
        if index in chosen:
            stressed.append(
                total_loss_r if rng.random() < total_loss_share else round(observed_loss, 4)
            )
        else:
            stressed.append(value)
    return stressed


def stress_table(
    rs: Sequence[float], config: RuinConfig, *,
    loss_rates: Sequence[float] = (0.15, 0.25, 0.35, 0.45),
) -> list[dict[str, Any]]:
    """P(halt) and growth at realistic loss rates, at the configured size."""
    rows: list[dict[str, Any]] = []
    for rate in loss_rates:
        pool = reclassify_losses(rs, loss_rate=rate, seed=config.seed)
        summary = simulate(pool, config).summary()
        mean_r = sum(pool) / len(pool)
        rows.append({
            "loss_rate": rate,
            "mean_r": round(mean_r, 4),
            "p_halt": summary["p_halt"],
            "p_ruin": summary["p_ruin"],
            "p_down": summary["p_down"],
            "median_growth": summary["median_growth"],
            "p05_growth": summary["p05_growth"],
            "median_max_drawdown_pct": summary["median_max_drawdown_pct"],
        })
    return rows


def win_haircut_table(
    rs: Sequence[float], config: RuinConfig, *,
    loss_rate: float = 0.35, haircuts: Sequence[float] = (0.0, 0.2, 0.4, 0.6, 0.8),
) -> list[dict[str, Any]]:
    """Degrade only the WIN side: the axis the corpus is actually fragile on.

    Reclassifying wins as losses barely moves the mean, because the wins average +1.12R
    against losses averaging -0.23R. Cutting what a win pays is the perturbation that decides
    whether the edge is real, and it is also the most likely way the record is wrong: exits
    described by the trader rather than filled at a venue are optimistic on the upside.
    """
    pool = reclassify_losses(rs, loss_rate=loss_rate, seed=config.seed)
    rows: list[dict[str, Any]] = []
    for haircut in haircuts:
        degraded = [r * (1.0 - haircut) if r > 0 else r for r in pool]
        summary = simulate(degraded, config).summary()
        rows.append({
            "win_haircut": haircut,
            "mean_r": round(sum(degraded) / len(degraded), 4),
            "p_down": summary["p_down"],
            "p_halt": summary["p_halt"],
            "median_growth": summary["median_growth"],
            "p05_growth": summary["p05_growth"],
        })
    return rows


def breakeven_win_haircut(
    rs: Sequence[float], *, loss_rate: float = 0.35, seed: int = 11,
) -> dict[str, Any]:
    """The win haircut at which expected R reaches zero, in closed form.

    mean = (1-L)*W*(1-h) + L*Lbar = 0  =>  h* = 1 + L*Lbar / ((1-L)*W)

    No search and no sampling error: this is the exact fraction of the average win the desk
    can fail to collect before the strategy is a coin flip at the stated loss rate.
    """
    pool = reclassify_losses(rs, loss_rate=loss_rate, seed=seed)
    wins = [r for r in pool if r > 0]
    losses = [r for r in pool if r < 0]
    if not wins or not losses:
        raise ValueError("need both wins and losses to solve for the breakeven haircut")
    share_loss = len(losses) / len(pool)
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)
    denominator = (1.0 - share_loss) * avg_win
    haircut = 1.0 + (share_loss * avg_loss) / denominator
    return {
        "loss_rate": loss_rate,
        "avg_win_r": round(avg_win, 4),
        "avg_loss_r": round(avg_loss, 4),
        "breakeven_win_haircut": round(haircut, 4),
        "note": (
            "wins can be overstated by this fraction before expected R is zero; a negative "
            "value means the stressed distribution is already unprofitable"
        ),
    }


def binding_size(
    rs: Sequence[float], config: RuinConfig, *,
    loss_rate: float, max_p_halt: float = 5.0,
    fractions: Sequence[float] = (0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03),
) -> dict[str, Any]:
    """The largest risk fraction that survives a stated loss rate.

    This is the number the sizing table should be built from: not the size that maximises
    growth on the claim, but the size that still clears the drawdown rule when the claim is
    wrong in the direction claims are usually wrong.
    """
    pool = reclassify_losses(rs, loss_rate=loss_rate, seed=config.seed)
    surviving: list[dict[str, Any]] = []
    for fraction in fractions:
        cfg = RuinConfig(
            start_equity=config.start_equity, risk_fraction=fraction,
            halt_drawdown=config.halt_drawdown, ruin_floor=config.ruin_floor,
            trades_per_run=config.trades_per_run, runs=config.runs, seed=config.seed,
            haircut=config.haircut,
        )
        summary = simulate(pool, cfg).summary()
        surviving.append({
            "risk_fraction": fraction,
            "p_halt": summary["p_halt"],
            "p_ruin": summary["p_ruin"],
            "median_growth": summary["median_growth"],
        })
    ok = [row for row in surviving if float(row["p_halt"]) <= max_p_halt]
    chosen = max(ok, key=lambda row: float(row["risk_fraction"])) if ok else None
    return {
        "loss_rate": loss_rate,
        "max_p_halt": max_p_halt,
        "supported_fraction": float(chosen["risk_fraction"]) if chosen else 0.0,
        "table": surviving,
    }


CAVEATS = (
    "returns are the trader's claimed exits, not fills: no fees, no slippage, no partial-fill "
    "shortfall",
    "the corpus contains only positions he chose to announce (survivorship toward his best "
    "trades)",
    "one contract was alerted twice inside 17 minutes and is counted once per row",
    "R is measured against the entry price, so an add at a better price is not modelled",
    "bootstrap resampling assumes the trades are exchangeable; a regime change breaks that",
    "a halted run stops earning, which understates the tail of a scenario that would have "
    "recovered",
)


def _diagnostics(
    rs: Sequence[float], edge: dict[str, Any], stress: Sequence[dict[str, Any]] = (),
) -> list[str]:
    """Say when the corpus cannot answer the question, instead of dressing it as an answer."""
    out: list[str] = []
    all_halts_zero = bool(stress) and all(float(row["p_halt"]) == 0.0 for row in stress)
    non_positive = sum(1 for r in rs if r < 0)
    rate = non_positive / len(rs)
    out.append(f"observed loss rate {100 * rate:.1f}% ({non_positive}/{len(rs)})")
    if edge.get("breakeven_haircut", 0.0) > 0.9:
        out.append(
            "breakeven haircut exceeds 90%: the claimed edge is far stronger than any real "
            "desk sustains, so this bound is a property of the claim, not evidence of safety - "
            "read the stress table instead"
        )
    if rate < 0.2:
        out.append(
            "a sub-20% loss rate on long options is below what a filled desk experiences; the "
            "stress table reclassifies wins as losses to reach realistic rates"
        )
    if all_halts_zero:
        out.append(
            "the drawdown rule does not bind at this size - long losing streaks are too rare "
            "for a 20% peak-to-trough stop to fire; judge the size on p_down instead"
        )
    if min(rs) > -0.5 and min(rs) < 0:
        out.append(
            f"the corpus contains no total loss (worst {min(rs):+.2f}R); stressed scenarios "
            "inject -1.0R outcomes, which is the most common result of a 0DTE long option"
        )
    return out


def report(rs: Sequence[float], config: RuinConfig) -> dict[str, Any]:
    """Everything a human needs to audit the number, including why it is optimistic."""
    base = simulate(rs, config).summary()
    edge = breakeven_haircut(rs, config)
    kelly_stats = kelly(rs)
    stress = stress_table(rs, config)
    wins = win_haircut_table(rs, config)
    edge_breakeven = breakeven_win_haircut(rs, loss_rate=0.35)
    sized = binding_size(rs, config, loss_rate=0.35)
    return {
        "simulation": base,
        "kelly": kelly_stats,
        "breakeven": edge,
        "stress": stress,
        "win_haircut": wins,
        "edge_breakeven": edge_breakeven,
        "binding_size_at_35pct_losses": sized,
        "recommended_fraction": sized["supported_fraction"],
        "diagnostics": _diagnostics(rs, edge, stress),
        "caveats": list(CAVEATS),
        "n_trades": len(rs),
    }


def from_ledger(ledger: Any, config: RuinConfig | None = None) -> dict[str, Any]:
    rs = [float(r) for r in ledger.rs()]
    return report(rs, config or RuinConfig())

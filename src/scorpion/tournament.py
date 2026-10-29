"""Out-of-sample policy tournaments: does a rule survive a walk-forward split?

Attribution measures one rule's delta on the corpus it was computed from. That is a necessary
number and not a sufficient one: a variant can win because it was selected after the whole path
was visible. This module turns the same production replay into a walk-forward tournament:

* folds are contiguous date blocks; the test block is strictly after the training blocks;
* a purge gap removes training trades whose outcomes could overlap the test window;
* each fold selects the best variant on training data and scores it on the unseen block;
* the selected out-of-sample path is compared with the incumbent baseline;
* Deflated Sharpe and the Probability of Backtest Overfitting price the selection itself;
* a paired block bootstrap puts an interval and a p-value on the difference;
* insufficient data is a refusal, not a small sample dressed as a result.

The unit is the realised R of a trade as the production executor actually took it. A refused
trade is 0R (no capital deployed), which is the honest choice for a policy comparison and is
reported separately as coverage. The report never claims statistical significance that the
corpus cannot support; it says which gate refused and why.
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .attribution import BASELINE, Variant, _haircut_ledger, _policy
from .corpus_replay import ReplayResult, replay

EULER_GAMMA = 0.5772156649015329
DEFAULT_SEED = 20260908
DEFAULT_FOLDS = 4
DEFAULT_PURGE_DAYS = 1
DEFAULT_EMBARGO_DAYS = 0
DEFAULT_MIN_TOTAL_TRADES = 30
DEFAULT_MIN_FOLD_TRADES = 4
DEFAULT_MIN_FOLD_TEST_TRADES = 3
DEFAULT_DSR_CONFIDENCE = 0.95
DEFAULT_MAX_PBO = 0.50
DEFAULT_ALPHA = 0.05
DEFAULT_BOOTSTRAP_DRAWS = 2_000


class TournamentError(ValueError):
    """A tournament that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Fold:
    """One walk-forward split: train strictly before test, with named exclusions."""

    index: int
    train_days: tuple[date, ...]
    test_days: tuple[date, ...]
    purged_days: tuple[date, ...] = ()
    embargoed_days: tuple[date, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "train_first": self.train_days[0].isoformat() if self.train_days else None,
            "train_last": self.train_days[-1].isoformat() if self.train_days else None,
            "test_first": self.test_days[0].isoformat() if self.test_days else None,
            "test_last": self.test_days[-1].isoformat() if self.test_days else None,
            "train_days": len(self.train_days),
            "test_days": len(self.test_days),
            "purged_days": [day.isoformat() for day in self.purged_days],
            "embargoed_days": [day.isoformat() for day in self.embargoed_days],
        }


@dataclass(frozen=True, slots=True)
class VariantRun:
    """The production replay of one variant, reduced to per-trade realised R."""

    variant: Variant
    r_by_identity: dict[str, float]
    taken_by_identity: dict[str, bool]
    summary: dict[str, Any]
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant.name,
            "note": self.variant.note,
            "trades": len(self.r_by_identity),
            "taken": sum(self.taken_by_identity.values()),
            "r_total": round(sum(self.r_by_identity.values()), 4),
            "verified": self.verified,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class FoldScore:
    fold: Fold
    selected: str
    train_r: dict[str, float]
    test_r: dict[str, float]
    selected_train_r: float
    selected_test_r: float
    baseline_test_r: float
    selection_margin: float
    test_trades: int
    selected_taken: int
    baseline_taken: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold": self.fold.index,
            "train_first": self.fold.train_days[0].isoformat() if self.fold.train_days else None,
            "test_first": self.fold.test_days[0].isoformat() if self.fold.test_days else None,
            "selected": self.selected,
            "selected_train_r": round(self.selected_train_r, 4),
            "selected_test_r": round(self.selected_test_r, 4),
            "baseline_test_r": round(self.baseline_test_r, 4),
            "selection_margin": round(self.selection_margin, 4),
            "test_trades": self.test_trades,
            "selected_taken": self.selected_taken,
            "baseline_taken": self.baseline_taken,
            "train_r": {name: round(value, 4) for name, value in self.train_r.items()},
            "test_r": {name: round(value, 4) for name, value in self.test_r.items()},
        }


@dataclass(frozen=True, slots=True)
class DeflatedSharpe:
    sharpe: float
    expected_max_sharpe: float
    deflated_sharpe: float
    observations: int
    trials: int
    skew: float
    kurtosis: float
    refused: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sharpe": round(self.sharpe, 4),
            "expected_max_sharpe": round(self.expected_max_sharpe, 4),
            "deflated_sharpe": round(self.deflated_sharpe, 4),
            "observations": self.observations,
            "trials": self.trials,
            "skew": round(self.skew, 4),
            "kurtosis": round(self.kurtosis, 4),
            "refused": self.refused,
        }


@dataclass(frozen=True, slots=True)
class PboResult:
    pbo: float
    splits: int
    sampled: bool
    median_oos_rank: float
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pbo": round(self.pbo, 4),
            "splits": self.splits,
            "sampled": self.sampled,
            "median_oos_rank": round(self.median_oos_rank, 4),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    observed_edge: float
    low: float
    high: float
    p_value: float
    draws: int
    block: int
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_edge": round(self.observed_edge, 4),
            "low": round(self.low, 4),
            "high": round(self.high, 4),
            "p_value": round(self.p_value, 4),
            "draws": self.draws,
            "block": self.block,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class TournamentResult:
    verdict: str
    reasons: tuple[str, ...]
    folds: tuple[FoldScore, ...]
    selection_counts: dict[str, int]
    variants: tuple[VariantRun, ...]
    oos_returns: tuple[float, ...]
    baseline_returns: tuple[float, ...]
    oos_total_r: float
    baseline_total_r: float
    oos_mean_r: float
    baseline_mean_r: float
    oos_edge: float
    selected_taken: int
    baseline_taken: int
    test_trades: int
    deflated: DeflatedSharpe
    pbo: PboResult
    bootstrap: BootstrapResult
    caveats: tuple[str, ...] = ()
    method: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "folds": [fold.to_dict() for fold in self.folds],
            "selection_counts": dict(sorted(self.selection_counts.items())),
            "variants": [run.to_dict() for run in self.variants],
            "oos_total_r": round(self.oos_total_r, 4),
            "baseline_total_r": round(self.baseline_total_r, 4),
            "oos_mean_r": round(self.oos_mean_r, 4),
            "baseline_mean_r": round(self.baseline_mean_r, 4),
            "oos_edge": round(self.oos_edge, 4),
            "coverage": {
                "test_trades": self.test_trades,
                "selected_taken": self.selected_taken,
                "baseline_taken": self.baseline_taken,
            },
            "deflated_sharpe": self.deflated.to_dict(),
            "pbo": self.pbo.to_dict(),
            "bootstrap": self.bootstrap.to_dict(),
            "caveats": list(self.caveats),
            "method": self.method,
        }


# -- statistics ---------------------------------------------------------------
def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _sample_std(values: Sequence[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def _skew(values: Sequence[float]) -> float:
    if len(values) < 3:
        return 0.0
    average = _mean(values)
    sigma = _sample_std(values)
    if sigma == 0.0:
        return 0.0
    return sum(((value - average) / sigma) ** 3 for value in values) / len(values)


def _kurtosis(values: Sequence[float]) -> float:
    if len(values) < 4:
        return 3.0
    average = _mean(values)
    sigma = _sample_std(values)
    if sigma == 0.0:
        return 3.0
    return sum(((value - average) / sigma) ** 4 for value in values) / len(values)


def _normal_ppf(probability: float) -> float:
    if not 0.0 < probability < 1.0:
        raise TournamentError("bad_probability", str(probability))
    return statistics.NormalDist().inv_cdf(probability)


def _normal_cdf(value: float) -> float:
    return statistics.NormalDist().cdf(value)


def deflated_sharpe(
    returns: Sequence[float],
    *,
    trials: int,
    minimum_observations: int = 20,
) -> DeflatedSharpe:
    """Bailey/Lopez de Prado's deflated Sharpe for the selected out-of-sample path.

    The selection itself is the risk. With one trial the expected maximum is zero; with N
    trials the best of N noisy Sharpes has a positive expectation, so the observed Sharpe is
    compared with that expectation rather than with zero.
    """
    if trials < 1:
        raise TournamentError("bad_trials", str(trials))
    if len(returns) < minimum_observations:
        return DeflatedSharpe(0.0, 0.0, 0.0, len(returns), trials, 0.0, 0.0,
                              refused="insufficient_observations")
    sigma = _sample_std(returns)
    if sigma == 0.0:
        return DeflatedSharpe(0.0, 0.0, 0.0, len(returns), trials, 0.0, 0.0,
                              refused="zero_variance")
    sharpe = _mean(returns) / sigma
    if trials == 1:
        expected_max = 0.0
    else:
        quantile = (
            (1.0 - EULER_GAMMA) * _normal_ppf(1.0 - 1.0 / trials)
            + EULER_GAMMA * _normal_ppf(1.0 - 1.0 / (trials * math.e))
        )
        expected_max = quantile / math.sqrt(max(1, len(returns) - 1))
    skew = _skew(returns)
    kurt = _kurtosis(returns)
    variance = 1.0 - skew * sharpe + ((kurt - 1.0) / 4.0) * sharpe * sharpe
    if variance <= 0.0:
        return DeflatedSharpe(sharpe, expected_max, 0.0, len(returns), trials, skew, kurt,
                              refused="degenerate_variance")
    statistic = (sharpe - expected_max) * math.sqrt(len(returns) - 1) / math.sqrt(variance)
    return DeflatedSharpe(
        sharpe=sharpe,
        expected_max_sharpe=expected_max,
        deflated_sharpe=_normal_cdf(statistic),
        observations=len(returns),
        trials=trials,
        skew=skew,
        kurtosis=kurt,
    )


def probability_of_backtest_overfitting(
    returns_by_variant: Mapping[str, Sequence[float]],
    *,
    n_splits: int = 8,
    max_splits: int = 2_000,
    seed: int = DEFAULT_SEED,
) -> PboResult:
    """CSCV: choose on half the blocks, rank the choice on the other half.

    The matrix is aligned by trade identity before this function is called, so every variant
    saw the same path. A selected variant that lands below the median out of sample is the
    definition of overfitting; the fraction of splits where that happens is the PBO.
    """
    names = tuple(returns_by_variant)
    if len(names) < 2:
        raise TournamentError("too_few_variants", str(len(names)))
    lengths = {len(returns_by_variant[name]) for name in names}
    if len(lengths) != 1:
        raise TournamentError("ragged_matrix", f"lengths {sorted(lengths)}")
    observations = lengths.pop()
    if n_splits < 4 or n_splits % 2:
        raise TournamentError("bad_splits", "n_splits must be even and at least 4")
    if observations < n_splits * 2:
        raise TournamentError(
            "insufficient_observations", f"{observations} < {n_splits * 2}"
        )

    bounds: list[tuple[int, int]] = []
    for index in range(n_splits):
        start = (index * observations) // n_splits
        end = ((index + 1) * observations) // n_splits
        bounds.append((start, end))

    combinations = list(itertools.combinations(range(n_splits), n_splits // 2))
    sampled = len(combinations) > max_splits
    if sampled:
        combinations = random.Random(seed).sample(combinations, max_splits)

    overfit = 0
    ranks: list[float] = []
    for chosen_blocks in combinations:
        in_sample = set(chosen_blocks)
        out_sample = set(range(n_splits)) - in_sample
        in_means: dict[str, float] = {}
        out_means: dict[str, float] = {}
        for name in names:
            series = returns_by_variant[name]
            in_values = [
                series[index]
                for block in in_sample
                for index in range(*bounds[block])
            ]
            out_values = [
                series[index]
                for block in out_sample
                for index in range(*bounds[block])
            ]
            in_means[name] = _mean(in_values)
            out_means[name] = _mean(out_values)
        selected = max(names, key=lambda name: (in_means[name], name))
        selected_out = out_means[selected]
        rank = float(
            sum(
                1
                for name in names
                if name != selected and out_means[name] < selected_out
            )
        )
        rank += 0.5 * sum(
            1
            for name in names
            if name != selected and out_means[name] == selected_out
        )
        ranks.append(rank)
        if rank < (len(names) - 1) / 2.0:
            overfit += 1

    return PboResult(
        pbo=overfit / len(combinations),
        splits=len(combinations),
        sampled=sampled,
        median_oos_rank=_mean(ranks),
        note=(
            "CSCV over contiguous blocks; a selected variant below the out-of-sample median "
            "counts as overfit"
        ),
    )


def paired_block_bootstrap(
    selected: Sequence[float],
    baseline: Sequence[float],
    *,
    draws: int = DEFAULT_BOOTSTRAP_DRAWS,
    seed: int = DEFAULT_SEED,
    block: int | None = None,
) -> BootstrapResult:
    """Paired block bootstrap of the selected-minus-baseline mean R.

    Pairing is by trade identity: both paths saw the same corpus, so the difference is the
    policy effect rather than two unrelated draws. Blocks preserve short-range dependence.
    """
    if len(selected) != len(baseline):
        raise TournamentError("ragged_pairs", f"{len(selected)} != {len(baseline)}")
    if not selected:
        raise TournamentError("no_observations", "empty paired series")
    if draws < 1:
        raise TournamentError("bad_draws", str(draws))
    differences = [left - right for left, right in zip(selected, baseline, strict=True)]
    observed = _mean(differences)
    width = block or max(1, round(math.sqrt(len(differences))))
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(draws):
        sample: list[float] = []
        while len(sample) < len(differences):
            start = rng.randrange(len(differences))
            for offset in range(width):
                sample.append(differences[(start + offset) % len(differences)])
                if len(sample) == len(differences):
                    break
        means.append(_mean(sample))
    means.sort()
    low = means[max(0, int(0.025 * draws) - 1)]
    high = means[min(draws - 1, int(0.975 * draws))]
    p_value = (1 + sum(1 for value in means if value <= 0.0)) / (draws + 1)
    return BootstrapResult(
        observed_edge=observed,
        low=low,
        high=high,
        p_value=min(1.0, p_value),
        draws=draws,
        block=width,
        note=(
            "paired stationary block bootstrap; p-value is the share of resampled edges at or "
            "below zero, smoothed by one"
        ),
    )


# -- folds --------------------------------------------------------------------
def build_folds(
    days: Iterable[date],
    *,
    n_folds: int = DEFAULT_FOLDS,
    purge_days: int = DEFAULT_PURGE_DAYS,
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
) -> tuple[Fold, ...]:
    """Contiguous walk-forward folds with a purge gap and an embargo gap.

    The first block is training only. Each later block is a test set, and its training set is
    every earlier day outside the purge gap. A fold whose train or test side is empty is
    dropped rather than scored on nothing.
    """
    if n_folds < 1:
        raise TournamentError("bad_folds", str(n_folds))
    if purge_days < 0 or embargo_days < 0:
        raise TournamentError("bad_gap", f"purge={purge_days} embargo={embargo_days}")
    unique = sorted(set(days))
    if len(unique) < n_folds + 1:
        raise TournamentError(
            "insufficient_days", f"{len(unique)} days for {n_folds} folds"
        )
    blocks: list[list[date]] = [[] for _ in range(n_folds + 1)]
    for index, day in enumerate(unique):
        blocks[(index * (n_folds + 1)) // len(unique)].append(day)

    folds: list[Fold] = []
    for index in range(1, n_folds + 1):
        test_block = blocks[index]
        if not test_block:
            continue
        test_start = test_block[0]
        purge_cutoff = test_start - timedelta(days=purge_days)
        all_prior = [day for block in blocks[:index] for day in block]
        train_days = tuple(day for day in all_prior if day < purge_cutoff)
        purged = tuple(day for day in all_prior if day >= purge_cutoff)
        embargo_cutoff = test_start + timedelta(days=embargo_days)
        test_days = tuple(day for day in test_block if day >= embargo_cutoff)
        embargoed = tuple(day for day in test_block if day < embargo_cutoff)
        if not train_days or not test_days:
            continue
        folds.append(
            Fold(
                index=len(folds) + 1,
                train_days=train_days,
                test_days=test_days,
                purged_days=purged,
                embargoed_days=embargoed,
            )
        )
    return tuple(folds)


# -- production replay --------------------------------------------------------
def run_variant(ledger: Any, variant: Variant, *, db_path: str | Path) -> VariantRun:
    """Replay one variant through the real executor and reduce it to per-trade R."""
    target = _haircut_ledger(ledger, variant.exit_haircut) if variant.exit_haircut else ledger
    result: ReplayResult = replay(
        target,
        db_path=db_path,
        policy=_policy(variant.policy_overrides),
        entry_slippage=variant.entry_slippage,
    )
    r_by_identity: dict[str, float] = {}
    taken_by_identity: dict[str, bool] = {}
    for trade in result.trades:
        r_by_identity[trade.identity] = (
            float(trade.executed_r) if trade.executed_r is not None else 0.0
        )
        taken_by_identity[trade.identity] = trade.status == "taken"
    return VariantRun(
        variant=variant,
        r_by_identity=r_by_identity,
        taken_by_identity=taken_by_identity,
        summary=result.summary(),
        verified=bool(result.verification.get("ok")),
    )


def _dedupe_variants(variants: Sequence[Variant]) -> tuple[Variant, ...]:
    by_name: dict[str, Variant] = {BASELINE.name: BASELINE}
    for variant in variants:
        if variant.name == BASELINE.name:
            by_name[BASELINE.name] = variant
        else:
            by_name[variant.name] = variant
    return tuple(by_name.values())


def _trade_day(ledger: Any) -> dict[str, date]:
    return {trade.identity: trade.ts_utc.date() for trade in ledger.trades}


def _mean_for(ids: Sequence[str], run: VariantRun) -> float:
    return _mean([run.r_by_identity[identity] for identity in ids])


def _select(train_r: Mapping[str, float], names: Sequence[str]) -> tuple[str, float]:
    ordered = sorted(names, key=lambda name: (-train_r[name], name))
    selected = ordered[0]
    second = train_r[ordered[1]] if len(ordered) > 1 else 0.0
    return selected, train_r[selected] - second


def tournament(
    ledger: Any,
    variants: Sequence[Variant] = (),
    *,
    db_path_for: Callable[[str], str | Path],
    n_folds: int = DEFAULT_FOLDS,
    purge_days: int = DEFAULT_PURGE_DAYS,
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
    min_total_trades: int = DEFAULT_MIN_TOTAL_TRADES,
    min_fold_trades: int = DEFAULT_MIN_FOLD_TRADES,
    min_fold_test_trades: int = DEFAULT_MIN_FOLD_TEST_TRADES,
    bootstrap_draws: int = DEFAULT_BOOTSTRAP_DRAWS,
    seed: int = DEFAULT_SEED,
    dsr_confidence: float = DEFAULT_DSR_CONFIDENCE,
    max_pbo: float = DEFAULT_MAX_PBO,
    alpha: float = DEFAULT_ALPHA,
) -> TournamentResult:
    """Run the walk-forward tournament over the production executor.

    Every candidate is replayed once. Folds then partition those same trades by entry day, so
    the cost is one replay per variant rather than one per variant per fold. A trade the policy
    refused contributes 0R and is counted in the coverage block, never silently dropped.
    """
    candidates = _dedupe_variants(tuple(variants))
    if len(candidates) < 2:
        raise TournamentError("too_few_variants", str(len(candidates)))
    if min_total_trades < 1 or min_fold_trades < 1 or min_fold_test_trades < 1:
        raise TournamentError("bad_minimum", "trade floors must be positive")
    if not 0.0 < dsr_confidence < 1.0 or not 0.0 <= max_pbo <= 1.0:
        raise TournamentError("bad_threshold", f"dsr={dsr_confidence} pbo={max_pbo}")

    runs = tuple(
        run_variant(ledger, variant, db_path=db_path_for(variant.name))
        for variant in candidates
    )
    run_by_name = {run.variant.name: run for run in runs}
    identities = [trade.identity for trade in ledger.trades]
    for run in runs:
        missing = set(identities) - set(run.r_by_identity)
        if missing:
            raise TournamentError(
                "incomplete_replay", f"{run.variant.name} missing {len(missing)} trades"
            )

    day_by_identity = _trade_day(ledger)
    names = tuple(run.variant.name for run in runs)
    fold_scores: list[FoldScore] = []
    oos_returns: list[float] = []
    baseline_returns: list[float] = []
    oos_identities: list[str] = []
    selected_taken = 0
    baseline_taken = 0

    folds = build_folds(
        day_by_identity.values(),
        n_folds=n_folds,
        purge_days=purge_days,
        embargo_days=embargo_days,
    )
    for fold in folds:
        train_set = set(fold.train_days)
        test_set = set(fold.test_days)
        train_ids = [identity for identity in identities if day_by_identity[identity] in train_set]
        test_ids = [identity for identity in identities if day_by_identity[identity] in test_set]
        if len(train_ids) < min_fold_trades or len(test_ids) < min_fold_test_trades:
            continue
        train_r = {name: _mean_for(train_ids, run_by_name[name]) for name in names}
        test_r = {name: _mean_for(test_ids, run_by_name[name]) for name in names}
        selected, margin = _select(train_r, names)
        selected_run = run_by_name[selected]
        baseline_run = run_by_name[BASELINE.name]
        selected_test = test_r[selected]
        baseline_test = test_r[BASELINE.name]
        fold_scores.append(
            FoldScore(
                fold=fold,
                selected=selected,
                train_r=train_r,
                test_r=test_r,
                selected_train_r=train_r[selected],
                selected_test_r=selected_test,
                baseline_test_r=baseline_test,
                selection_margin=margin,
                test_trades=len(test_ids),
                selected_taken=sum(
                    selected_run.taken_by_identity[identity] for identity in test_ids
                ),
                baseline_taken=sum(
                    baseline_run.taken_by_identity[identity] for identity in test_ids
                ),
            )
        )
        for identity in test_ids:
            oos_identities.append(identity)
            oos_returns.append(selected_run.r_by_identity[identity])
            baseline_returns.append(baseline_run.r_by_identity[identity])
            selected_taken += int(selected_run.taken_by_identity[identity])
            baseline_taken += int(baseline_run.taken_by_identity[identity])

    reasons: list[str] = []
    if len(identities) < min_total_trades:
        reasons.append(f"insufficient_trades:{len(identities)}<{min_total_trades}")
    if not fold_scores:
        reasons.append("no_eligible_folds")
    if oos_identities and len(set(oos_identities)) != len(oos_identities):
        reasons.append("overlapping_test_folds")

    selection_counts: dict[str, int] = {name: 0 for name in names}
    for score in fold_scores:
        selection_counts[score.selected] += 1

    oos_total = sum(oos_returns)
    baseline_total = sum(baseline_returns)
    oos_mean = _mean(oos_returns)
    baseline_mean = _mean(baseline_returns)
    edge = oos_mean - baseline_mean
    deflated = deflated_sharpe(oos_returns, trials=len(names))
    try:
        pbo = (
            probability_of_backtest_overfitting(
                {
                    name: [run_by_name[name].r_by_identity[identity] for identity in identities]
                    for name in names
                },
                seed=seed,
            )
            if len(names) >= 2 and len(identities) >= 16
            else PboResult(1.0, 0, False, 0.0, "too few trades for CSCV")
        )
    except TournamentError as exc:
        pbo = PboResult(1.0, 0, False, 0.0, f"{exc.code}: {exc.detail}")
    try:
        bootstrap = paired_block_bootstrap(
            oos_returns,
            baseline_returns,
            draws=bootstrap_draws,
            seed=seed,
        )
    except TournamentError as exc:
        bootstrap = BootstrapResult(edge, edge, edge, 1.0, 0, 0, f"{exc.code}: {exc.detail}")

    incumbent_every_fold = (
        bool(fold_scores)
        and selection_counts.get(BASELINE.name, 0) == len(fold_scores)
    )
    if reasons:
        verdict = "REFUSED"
    elif edge < 0.0:
        verdict = "REJECTED"
        reasons.append("out_of_sample_edge_not_positive")
    elif edge == 0.0 and incumbent_every_fold:
        verdict = "INCUMBENT_HELD"
    elif edge <= 0.0:
        verdict = "REJECTED"
        reasons.append("out_of_sample_edge_not_positive")
    elif deflated.deflated_sharpe < dsr_confidence:
        verdict = "FRAGILE"
        reasons.append(
            f"deflated_sharpe:{deflated.deflated_sharpe:.4f}<{dsr_confidence:.4f}"
        )
    elif pbo.pbo > max_pbo:
        verdict = "FRAGILE"
        reasons.append(f"pbo:{pbo.pbo:.4f}>{max_pbo:.4f}")
    elif bootstrap.p_value >= alpha:
        verdict = "FRAGILE"
        reasons.append(f"paired_p_value:{bootstrap.p_value:.4f}>={alpha:.4f}")
    else:
        verdict = "SUPPORTED"

    if incumbent_every_fold:
        reasons.append("incumbent_selected_every_fold")

    caveats = [
        "folds are chronological, but the corpus is small and not a random sample of future days",
        "a refused trade is scored 0R: the policy avoided capital, not the opportunity",
        "Deflated Sharpe assumes a trial count equal to the variants tested in this run",
        "the bootstrap preserves short blocks of dependence, not regime changes",
        "PBO is a selection diagnostic, never a guarantee of future performance",
    ]
    method = {
        "corpus_trades": len(identities),
        "variants": len(names),
        "folds_requested": n_folds,
        "folds_scored": len(fold_scores),
        "purge_days": purge_days,
        "embargo_days": embargo_days,
        "min_total_trades": min_total_trades,
        "min_fold_trades": min_fold_trades,
        "min_fold_test_trades": min_fold_test_trades,
        "bootstrap_draws": bootstrap_draws,
        "seed": seed,
        "selection_rule": "highest mean realised R on the training block",
        "test_rule": "selected variant applied unchanged to the unseen block",
    }
    return TournamentResult(
        verdict=verdict,
        reasons=tuple(reasons),
        folds=tuple(fold_scores),
        selection_counts=selection_counts,
        variants=runs,
        oos_returns=tuple(oos_returns),
        baseline_returns=tuple(baseline_returns),
        oos_total_r=oos_total,
        baseline_total_r=baseline_total,
        oos_mean_r=oos_mean,
        baseline_mean_r=baseline_mean,
        oos_edge=edge,
        selected_taken=selected_taken,
        baseline_taken=baseline_taken,
        test_trades=len(oos_identities),
        deflated=deflated,
        pbo=pbo,
        bootstrap=bootstrap,
        caveats=tuple(caveats),
        method=method,
    )


def default_variants() -> tuple[Variant, ...]:
    """A small, high-signal set for the default tournament.

    Each one answers a question the desk has actually argued about: the pipe test, the
    dislocation gate, the markup, a worse exit, and entry latency. `attribution.default_variants`
    remains the full menu; a tournament pays for every replay.
    """
    return (
        Variant("no_pipe_test", {"first_entry_quantity": 10},
                note="size the first entry like every other entry"),
        Variant("wider_gate", {"max_dislocation_from_reference": 0.40},
                note="accept an ask up to 40% off the bid instead of 25%"),
        Variant("no_markup", {"price_limit_markup": 0.00},
                note="bid his price exactly, never 15% above"),
        Variant("exit_haircut_10", exit_haircut=0.10,
                note="exits fill 10% worse than claimed"),
        Variant("entry_slip_20", entry_slippage=0.20,
                note="pay 20% above the alert bid, as a late reader would"),
    )


def render(payload: dict[str, Any]) -> str:
    lines = [
        f"verdict {payload['verdict']}  edge {payload['oos_edge']:+.4f}R/trade  "
        f"oos {payload['oos_total_r']:+.2f}R vs baseline {payload['baseline_total_r']:+.2f}R",
        "",
        f"{'fold':>4}  {'test first':10}  {'selected':18}  {'train R':>8}  "
        f"{'test R':>8}  {'base R':>8}",
    ]
    for fold in payload["folds"]:
        lines.append(
            f"{fold['fold']:>4}  {str(fold['test_first']):10}  {fold['selected']:18}  "
            f"{fold['selected_train_r']:+8.3f}  {fold['selected_test_r']:+8.3f}  "
            f"{fold['baseline_test_r']:+8.3f}"
        )
    dsr = payload["deflated_sharpe"]
    pbo = payload["pbo"]
    boot = payload["bootstrap"]
    lines.append("")
    lines.append(
        f"deflated Sharpe {dsr['deflated_sharpe']:.3f} (SR {dsr['sharpe']:+.3f}, "
        f"expected max {dsr['expected_max_sharpe']:+.3f}, trials {dsr['trials']})"
    )
    lines.append(
        f"PBO {pbo['pbo']:.3f} over {pbo['splits']} splits  "
        f"paired edge {boot['observed_edge']:+.4f}R  "
        f"95% [{boot['low']:+.4f}, {boot['high']:+.4f}]  p={boot['p_value']:.4f}"
    )
    if payload["reasons"]:
        lines.append("reasons " + ", ".join(payload["reasons"]))
    lines.append("caveats")
    for caveat in payload["caveats"]:
        lines.append(f"  - {caveat}")
    return "\n".join(lines)

"""Attribution: what each rule is worth, measured through the real pipeline.

The corpus has supported claims like "the pipe test cost us" and "the 25% gate is too tight" since
the first backtest. They were arguments, not measurements, because a rule can only be priced by
re-running the SAME corpus through the SAME executor with only that rule changed.

That is what this does. Each variant is the production policy with one field changed (or one exit
haircut applied), replayed through the real parser, reducer, authorization, outbox and settlement.
The delta against the baseline is that rule's contribution, and it is exact rather than sampled -
the corpus is deterministic, so no confidence interval is needed or offered.

Three properties make the numbers trustworthy:

* **One rule at a time.** A variant differs from the baseline in exactly one dimension. A variant
  that changes two things produces a delta nobody can attribute.
* **The same clock, the same book.** Every variant replays from a fresh store so no variant inherits
  another's positions, and the ledger order is identical across all of them.
* **The delta must compose.** If two variants' deltas are computed independently, their sum is only
  meaningful when each was measured against the same baseline; the report states the baseline
  explicitly rather than leaving it implicit.

Deliberately not included: statistical significance. Ten corpus variants of 88 trades cannot support
it, and printing a p-value on a deterministic replay would be theatre. The honest statement is the
point estimate plus the sample it came from.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import Policy
from .corpus_replay import replay

D = Decimal


class VariantError(ValueError):
    """A variant that cannot be attributed, because it changes more than one thing."""


@dataclass(frozen=True, slots=True)
class Variant:
    """The production policy with at most one dimension changed."""

    name: str
    policy_overrides: dict[str, Any] = field(default_factory=dict)
    exit_haircut: float = 0.0          # multiply every claimed exit price by (1 - haircut)
    entry_slippage: float = 0.0        # pay this much more than the alert bid
    scenario: bool = False             # a matrix cell: several dimensions by design, not a delta
    note: str = ""

    def __post_init__(self) -> None:
        dimensions = 0
        if self.policy_overrides:
            dimensions += 1
        if self.exit_haircut:
            dimensions += 1
        if self.entry_slippage:
            dimensions += 1
        if dimensions > 1 and not self.scenario:
            raise VariantError(
                f"{self.name}: changes a policy field AND an exit haircut, so its delta is "
                "unattributable - use two variants"
            )
        if self.exit_haircut and not (0.0 <= self.exit_haircut < 1.0):
            raise VariantError(f"{self.name}: exit_haircut must be in [0, 1)")


BASELINE = Variant("baseline", note="the live policy, unchanged")


def _policy(overrides: dict[str, Any]) -> Policy:
    base = Policy(live_submission_enabled=True, skip_fridays=False, first_entry_quantity=1)
    if not overrides:
        return base
    known = {f.name for f in Policy.__dataclass_fields__.values()}
    unknown = set(overrides) - known
    if unknown:
        raise VariantError(f"unknown policy field(s): {sorted(unknown)}")
    return replace(base, **overrides)


def _haircut_ledger(ledger: Any, haircut: float) -> Any:
    """Scale every claimed exit by (1 - haircut), leaving entries and notes untouched."""
    factor = D(str(1.0 - haircut))
    trades = []
    for trade in ledger.trades:
        if trade.exit_price is not None:
            price = (trade.exit_price * factor).quantize(D("0.01"))
        elif trade.lifecycle.exit_pct is not None:
            entry = D(str(trade.entry_bid))
            price = (entry * (1 + D(trade.lifecycle.exit_pct) / 100 * factor)).quantize(D("0.01"))
        else:
            price = None
        trades.append(replace(trade, exit_price=price))
    return replace(ledger, trades=tuple(trades))


def run_variant(ledger: Any, variant: Variant, *, db_path: str | Path) -> dict[str, Any]:
    """Replay one variant and return its summary plus the verification result."""
    target = _haircut_ledger(ledger, variant.exit_haircut) if variant.exit_haircut else ledger
    result = replay(
        target, db_path=db_path, policy=_policy(variant.policy_overrides),
        entry_slippage=variant.entry_slippage,
    )
    summary = result.summary()
    return {
        "variant": variant.name,
        "note": variant.note,
        "taken": summary["taken"],
        "refused": summary["refused"],
        "contracts": summary["contracts"],
        "realised_usd": summary["realised_usd"],
        "r_total": float(summary["executed_r_total"]),
        "verified": summary["verified"],
        "refusals": summary["refusals"],
    }


def attribute(
    ledger: Any, variants: Sequence[Variant], *, db_path_for: Callable[[str], str | Path],
) -> dict[str, Any]:
    """Run the baseline and each variant, and state each one's contribution in R.

    `db_path_for` is a factory rather than a single path so every variant gets its own store: a
    variant that inherited the baseline's positions would measure the previous run, not the rule.
    """
    baseline = run_variant(ledger, BASELINE, db_path=db_path_for("baseline"))
    rows: list[dict[str, Any]] = []
    for variant in variants:
        outcome = run_variant(ledger, variant, db_path=db_path_for(variant.name))
        delta = round(outcome["r_total"] - baseline["r_total"], 4)
        rows.append({
            **outcome,
            "delta_r": delta,
            "delta_usd": round(float(outcome["realised_usd"]) - float(baseline["realised_usd"]), 2),
            "trades_delta": outcome["taken"] - baseline["taken"],
        })

    rows.sort(key=lambda row: row["delta_r"])
    return {
        "baseline": baseline,
        "variants": rows,
        "method": {
            "corpus_trades": len(ledger.trades),
            "one_dimension_per_variant": True,
            "store_per_variant": True,
            "note": (
                "deltas are exact for this corpus, not sampled: the replay is deterministic. They "
                "are not statistically significant and the report does not pretend otherwise"
            ),
        },
    }


def default_variants() -> tuple[Variant, ...]:
    """The rules worth pricing first, each one dimension."""
    return (
        Variant("no_pipe_test", {"first_entry_quantity": 10},
                note="size the first entry like every other entry"),
        Variant("wider_gate", {"max_dislocation_from_reference": D("0.40")},
                note="accept a ask up to 40% off the bid instead of 25%"),
        Variant("tighter_gate", {"max_dislocation_from_reference": D("0.10")},
                note="only take tight spreads"),
        Variant("no_markup", {"price_limit_markup": D("0.00")},
                note="bid his price exactly, never 15% above"),
        Variant("trade_fridays_off", {"skip_fridays": True},
                note="the live policy refuses Fridays; this prices that refusal"),
        Variant("three_slots", {"max_open_positions": 3},
                note="one more concurrent position"),
        Variant("exit_haircut_10", exit_haircut=0.10,
                note="exits fill 10% worse than claimed"),
        Variant("exit_haircut_25", exit_haircut=0.25,
                note="exits fill 25% worse than claimed"),
        Variant("entry_slip_20", entry_slippage=0.20,
                note="pay 20% above the alert bid, as a late reader would"),
        Variant("entry_slip_35", entry_slippage=0.35,
                note="pay 35% above it, past the gate"),
    )


def matrix(
    ledger: Any, *, db_path_for: Callable[[str], str | Path],
    slippages: Sequence[float] = (0.0, 0.10, 0.20, 0.35),
    gates: Sequence[Decimal] = (D("0.10"), D("0.25"), D("0.40")),
) -> dict[str, Any]:
    """Slippage x gate, because a gate can only be priced against the latency it refuses.

    One-at-a-time attribution answered "what does a 25% gate cost" with zero, because with no
    slippage there is nothing to refuse. The question that decides a config is conditional: given
    the latency we actually have, does a tighter gate make money? That is a two-dimensional
    question and it needs a two-dimensional answer.
    """
    rows: list[dict[str, Any]] = []
    for slippage in slippages:
        for gate in gates:
            name = f"slip{int(slippage * 100)}_gate{int(gate * 100)}"
            outcome = run_variant(
                ledger,
                Variant(name, {"max_dislocation_from_reference": gate},
                        entry_slippage=slippage, scenario=True),
                db_path=db_path_for(name),
            )
            rows.append({
                "slippage": slippage, "gate": float(gate),
                "taken": outcome["taken"], "r_total": outcome["r_total"],
                "realised_usd": outcome["realised_usd"],
                "verified": outcome["verified"],
            })

    best = max(rows, key=lambda row: row["r_total"]) if rows else None
    return {
        "rows": rows,
        "best": best,
        "note": (
            "each cell is a full corpus replay under that slippage and that gate; the best cell is "
            "the config this corpus supports, not a prediction"
        ),
    }


def render_matrix(payload: dict[str, Any]) -> str:
    rows = payload["rows"]
    if not rows:
        return "no rows"
    gates = sorted({row["gate"] for row in rows})
    slips = sorted({row["slippage"] for row in rows})
    lines = ["slippage | gate " + " ".join(f"{g:>9.0%}" for g in gates)]
    for slip in slips:
        cells = []
        for gate in gates:
            match = next(r for r in rows if r["slippage"] == slip and r["gate"] == gate)
            cells.append(f"{match['r_total']:>+9.2f}")
        lines.append(f"{slip:>13.0%} " + " ".join(cells))
    best = payload["best"]
    if best:
        lines.append(
            f"best: slippage {best['slippage']:.0%}, gate {best['gate']:.0%} -> "
            f"{best['r_total']:+.2f}R on {best['taken']} trades"
        )
    return chr(10).join(lines)


def render(payload: dict[str, Any]) -> str:
    base = payload["baseline"]
    lines = [
        f"baseline {base['r_total']:+.2f}R  {base['taken']} taken  "
        f"${base['realised_usd']} realised  verified {base['verified']}",
        "",
        f"{'variant':18s} {'delta R':>9s} {'delta $':>11s} {'trades':>7s}  note",
    ]
    for row in payload["variants"]:
        lines.append(
            f"{row['variant']:18s} {row['delta_r']:+9.2f} {row['delta_usd']:+11.2f} "
            f"{row['trades_delta']:+7d}  {row['note']}"
        )
    return "\n".join(lines)


def worst_and_best(payload: dict[str, Any]) -> dict[str, Any]:
    """The single most expensive rule and the single most valuable one, by measured delta."""
    rows: Iterable[dict[str, Any]] = payload["variants"]
    ordered = sorted(rows, key=lambda row: row["delta_r"])
    if not ordered:
        return {}
    return {"most_expensive": ordered[0], "most_valuable": ordered[-1]}

"""Regime join: splitting a policy delta by the conditions it was earned in.

`attribution.py` answers "what is this rule worth" across the whole corpus. That number hides the
question a desk actually asks: worth it *when*? A rule that pays in calm chop and bleeds in volatile
trend is not a rule with one value, it is a rule with two, and applying it unconditionally is a bet
that the regime mix stays as it was.

This module joins regime labels onto trades and re-expresses attribution per regime:

* every trade is tagged with its session's regime, or `unknown` when no bars exist;
* a delta is computed per regime only where BOTH the baseline and the variant have a usable
  sample, because a delta between a full cell and an empty one measures the sample, not the rule;
* coverage is reported in the same breath, since a per-regime table over 6 labelled trades is a
  description of the bars we happen to have.

The join is by contract key - ticker, side, strike, day - which both sides already agree on by
construction. No fuzzy matching, no date arithmetic, no timezone assumption beyond what `ledger.py`
settled: the corpus's clock is Pacific, proved by measurement.

What this deliberately does not do: pool regimes to manufacture significance. If four cells hold two
trades each, the honest output is four refusals, and the report says so.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

MIN_CELL = 5


class JoinError(ValueError):
    """A join that cannot be made, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Tagged:
    """One trade with the regime of its session."""

    identity: str
    contract_key: str
    day: date
    regime: str
    r_multiple: float | None
    channel: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity, "contract_key": self.contract_key,
            "day": self.day.isoformat(), "regime": self.regime,
            "r_multiple": None if self.r_multiple is None else round(self.r_multiple, 4),
            "channel": self.channel,
        }


def contract_key(trade: Any) -> str:
    return f"{trade.ticker}|{trade.side}|{trade.strike}|{trade.day.isoformat()}"


def tag(ledger: Any, classified: Mapping[str, Any]) -> list[Tagged]:
    """Attach a regime to every trade. Unlabelled sessions become `unknown`, never a default."""
    out: list[Tagged] = []
    for trade in ledger.trades:
        key = contract_key(trade)
        entry = classified.get(key)
        regime = getattr(entry, "regime", "unknown") if entry is not None else "unknown"
        r_multiple = trade.r_multiple
        out.append(Tagged(
            identity=str(getattr(trade, "identity", key)),
            contract_key=key, day=trade.day, regime=regime,
            r_multiple=None if r_multiple is None else float(r_multiple),
            channel=str(getattr(trade, "channel", "")),
        ))
    return out


def coverage(tagged: Sequence[Tagged]) -> dict[str, Any]:
    labelled = [t for t in tagged if t.regime != "unknown"]
    counts: dict[str, int] = {}
    for item in tagged:
        counts[item.regime] = counts.get(item.regime, 0) + 1
    return {
        "trades": len(tagged),
        "labelled": len(labelled),
        "unlabelled": len(tagged) - len(labelled),
        "labelled_pct": round(100.0 * len(labelled) / len(tagged), 1) if tagged else 0.0,
        "by_regime": dict(sorted(counts.items())),
    }


def cell_stats(tagged: Sequence[Tagged], regime: str, *,
               floor: int = MIN_CELL) -> dict[str, Any]:
    values = [t.r_multiple for t in tagged if t.regime == regime and t.r_multiple is not None]
    if len(values) < floor:
        return {"regime": regime, "n": len(values),
                "withheld": f"below the floor of {floor}"}
    wins = [v for v in values if v > 0]
    return {
        "regime": regime,
        "n": len(values),
        "mean_r": round(sum(values) / len(values), 4),
        "total_r": round(sum(values), 2),
        "win_rate": round(100.0 * len(wins) / len(values), 1),
    }


def per_regime(tagged: Sequence[Tagged], *, floor: int = MIN_CELL) -> dict[str, Any]:
    regimes = sorted({t.regime for t in tagged})
    return {regime: cell_stats(tagged, regime, floor=floor) for regime in regimes}


def delta_by_regime(
    baseline: Sequence[Tagged], variant: Sequence[Tagged], *, floor: int = MIN_CELL,
) -> dict[str, Any]:
    """A variant's contribution inside each regime, where both cells carry evidence.

    A delta is computed only where BOTH sides clear the floor. Subtracting an empty cell from a full
    one produces a number that describes the sample rather than the rule, and that number would look
    exactly like a finding.
    """
    base_by = {t.identity: t for t in baseline}
    rows: dict[str, Any] = {}
    for item in variant:
        rows.setdefault(item.regime, []).append(item)

    out: dict[str, Any] = {}
    for regime, items in sorted(rows.items()):
        base_values: list[float] = []
        var_values: list[float] = []
        for item in items:
            mine = item.r_multiple
            if mine is not None:
                var_values.append(mine)
            match = base_by.get(item.identity)
            theirs = match.r_multiple if match is not None else None
            if theirs is not None:
                base_values.append(theirs)
        if len(base_values) < floor or len(var_values) < floor:
            out[regime] = {
                "n_variant": len(var_values), "n_baseline": len(base_values),
                "withheld": f"both cells need {floor} observations",
            }
            continue
        delta = (sum(var_values) - sum(base_values)) / len(var_values)
        out[regime] = {
            "n_variant": len(var_values),
            "n_baseline": len(base_values),
            "delta_r_per_trade": round(delta, 4),
            "variant_mean_r": round(sum(var_values) / len(var_values), 4),
            "baseline_mean_r": round(sum(base_values) / len(base_values), 4),
        }
    return out


def compositions(tagged: Sequence[Tagged]) -> dict[str, Any]:
    """Regime mix overall and per channel - the mix is a risk, not just a label."""
    overall = coverage(tagged)
    by_channel: dict[str, dict[str, int]] = {}
    for item in tagged:
        bucket = by_channel.setdefault(item.channel or "unknown", {})
        bucket[item.regime] = bucket.get(item.regime, 0) + 1
    return {
        "overall": overall["by_regime"],
        "labelled_pct": overall["labelled_pct"],
        "by_channel": {k: dict(sorted(v.items())) for k, v in sorted(by_channel.items())},
    }


def report(
    ledger: Any, classified: Mapping[str, Any], *, floor: int = MIN_CELL,
    baseline: Sequence[Tagged] | None = None, variant: Sequence[Tagged] | None = None,
) -> dict[str, Any]:
    tagged = tag(ledger, classified)
    payload: dict[str, Any] = {
        "coverage": coverage(tagged),
        "regimes": per_regime(tagged, floor=floor),
        "compositions": compositions(tagged),
        "floor": floor,
        "caveats": [
            "a per-regime table describes the sessions whose bars were captured, not the market",
            "cells below the floor are withheld rather than pooled",
            "a delta needs evidence on BOTH sides; an empty baseline cell is not a zero",
        ],
    }
    if baseline is not None and variant is not None:
        payload["delta"] = delta_by_regime(baseline, variant, floor=floor)
    return payload


def render(payload: Mapping[str, Any]) -> str:
    cov = payload["coverage"]
    lines = [
        f"{cov['trades']} trades  {cov['labelled']} labelled ({cov['labelled_pct']}%)  "
        f"{cov['unlabelled']} unlabelled"
    ]
    for regime, cell in payload["regimes"].items():
        if "withheld" in cell:
            lines.append(f"  {regime:16s} n={cell['n']:3d}  withheld")
        else:
            lines.append(
                f"  {regime:16s} n={cell['n']:3d}  mean {cell['mean_r']:+.3f}R  "
                f"win {cell['win_rate']:5.1f}%"
            )
    for regime, cell in (payload.get("delta") or {}).items():
        if "withheld" in cell:
            lines.append(f"  delta {regime:16s} withheld")
        else:
            lines.append(
                f"  delta {regime:16s} {cell['delta_r_per_trade']:+.3f}R per trade"
            )
    return "\n".join(lines)


def headline(payload: Mapping[str, Any]) -> dict[str, Any]:
    cov = payload["coverage"]
    usable = [r for r, c in payload["regimes"].items() if "withheld" not in c]
    return {
        "trades": cov["trades"],
        "labelled_pct": cov["labelled_pct"],
        "regimes_with_evidence": len(usable),
        "best_regime": max(
            usable, key=lambda r: payload["regimes"][r]["mean_r"]
        ) if usable else None,
    }


def tagged_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[Tagged]:
    """Rebuild tagged trades from serialised rows, for comparing two runs offline."""
    out: list[Tagged] = []
    for row in rows:
        out.append(Tagged(
            identity=str(row["identity"]), contract_key=str(row["contract_key"]),
            day=date.fromisoformat(str(row["day"])), regime=str(row["regime"]),
            r_multiple=None if row.get("r_multiple") is None else float(row["r_multiple"]),
            channel=str(row.get("channel", "")),
        ))
    return out

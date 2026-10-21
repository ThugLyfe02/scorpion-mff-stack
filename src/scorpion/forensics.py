"""Refusal forensics: what the policy actually costs, in dollars, by rung.

The branch refuses trades and logs why, then argues about whether the refusal was right. That
argument is answerable from data already held: each refused trade carries the R the
executor would have earned had the rung not fired. Summing that by rung prices the policy.

Two distinctions this module keeps:

  * a refusal that avoided a loss is a SAVING and is reported as negative cost. Counting only the
    profitable refusals would make every rung look expensive and every loosening look free.
  * a refusal with no counterfactual is UNPRICED. It is counted and named, never imputed - a rung
    whose trades were never measured must not be credited with the average.

The point is not to rank rungs by annoyance. It is to answer one question per rung: if
removed tomorrow, what would the desk have made or lost, and on how many trades.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class ForensicsError(ValueError):
    """A forensics question that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


MIN_SAMPLE = 3


@dataclass
class Rung:
    """One refusal reason, priced."""

    name: str
    refusals: int = 0
    priced: int = 0
    avoided_loss: float = 0.0
    missed_gain: float = 0.0

    @property
    def net_cost(self) -> float:
        """Positive means the rung costs money; negative means it saves money."""
        return self.missed_gain - self.avoided_loss

    @property
    def charged(self) -> int:
        return self.refusals - self.priced

    def to_dict(self) -> dict[str, Any]:
        return {
            "rung": self.name,
            "refusals": self.refusals,
            "priced": self.priced,
            "unpriced": self.charged,
            "missed_gain_usd": round(self.missed_gain, 2),
            "avoided_loss_usd": round(self.avoided_loss, 2),
            "net_cost_usd": round(self.net_cost, 2),
            "verdict": (
                "unpriced" if self.priced == 0
                else "earning_its_keep" if self.net_cost < 0
                else "costing_money"
            ),
        }


@dataclass
class Report:
    rungs: dict[str, Rung] = field(default_factory=dict)
    trades: int = 0
    refusals: int = 0

    @property
    def priced(self) -> int:
        return sum(rung.priced for rung in self.rungs.values())

    @property
    def unpriced(self) -> int:
        return self.refusals - self.priced

    @property
    def total_net_cost(self) -> float:
        return sum(rung.net_cost for rung in self.rungs.values())

    def ranked(self) -> list[dict[str, Any]]:
        rows = [rung.to_dict() for rung in self.rungs.values()]
        return sorted(rows, key=lambda row: row["net_cost_usd"], reverse=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "refusals": self.refusals,
            "priced": self.priced,
            "unpriced": self.unpriced,
            "total_net_cost_usd": round(self.total_net_cost, 2),
            "rungs": self.ranked(),
            "caveats": [
                "the counterfactual assumes a fill at the alert premium; entry "
                "latency would have applied to it too, and that cost is measured separately",
                "a rung below three priced refusals is reported but not ranked as decisive",
                "net cost is a sum of R converted at the clip size of each trade, not a portfolio "
                "simulation - concurrent exposure is ignored",
            ],
        }


def _counterfactual(row: Mapping[str, Any]) -> float | None:
    value = row.get("counterfactual_r")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ForensicsError("bad_counterfactual", str(value)) from error


def attribute(rows: Sequence[Mapping[str, Any]], *, usd_per_r: float = 1500.0) -> Report:
    """Price every refusal by its rung. `usd_per_r` converts R to dollars at the clip size."""
    if usd_per_r <= 0:
        raise ForensicsError("bad_rate", str(usd_per_r))
    report = Report()
    for row in rows:
        rung_name = str(row.get("rung") or "unspecified")
        report.trades += 1
        report.refusals += 1
        rung = report.rungs.setdefault(rung_name, Rung(rung_name))
        rung.refusals += 1
        value = _counterfactual(row)
        if value is None:
            continue
        rung.priced += 1
        dollars = value * usd_per_r
        if dollars >= 0:
            rung.missed_gain += dollars
        else:
            rung.avoided_loss += -dollars
    return report


def decisive(report: Report, *, minimum: int = MIN_SAMPLE) -> list[dict[str, Any]]:
    """Rungs with enough priced refusals to argue about, worst first."""
    return [row for row in report.ranked() if row["priced"] >= minimum]


def loosening_case(report: Report, rung: str, *, minimum: int = MIN_SAMPLE) -> dict[str, Any]:
    """The case for removing one rung: how much it would have made, and on how many trades."""
    row = report.rungs.get(rung)
    if row is None:
        raise ForensicsError("unknown_rung", rung)
    enough = row.priced >= minimum
    return {
        "rung": rung,
        "verdict": (
            "keep" if not enough else
            ("loosen" if row.net_cost > 0 else "earning_its_keep")
        ),
        "net_cost_usd": round(row.net_cost, 2),
        "priced_refusals": row.priced,
        "note": (
            "too few priced refusals to decide; the rung is neither defended nor condemned here"
            if not enough
            else f"{row.priced} refusals would have produced "
                 f"{row.net_cost:+,.0f} dollars net"
        ),
    }


def render(report: Report) -> str:
    lines = [
        f"{report.refusals} refusals, {report.priced} priced, {report.unpriced} unpriced; "
        f"net cost {report.total_net_cost:+,.0f}"
    ]
    for row in report.ranked():
        lines.append(
            f"  {row['rung']:26s} n={row['refusals']:3d} net {row['net_cost_usd']:+10,.0f}  "
            f"{row['verdict']}"
        )
    return "\n".join(lines)

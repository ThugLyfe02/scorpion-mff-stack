"""Transaction cost analysis on realised fills.

The backtest assumptions that matter most are the ones nobody measures: how far through
the signal's bid we actually paid, how often a limit filled at all, and how long it took.
Those three numbers decide whether a 5% markup is honest or optimistic, and whether a
+25% dislocation gate ever binds in real conditions.

Everything here is computed from `executions` rows the dispatcher wrote when a venue
confirmed a fill, so it is measurement rather than modelling. Signals:

    cost_bps > 0            we paid worse than our own limit
    fill_ratio < 1          a partial - the size we booked is not the size we got
    latency_ms p90 > budget the hot path is not hot

The `suggested_multiple` is the p90 of `fill_price / reference` rounded **up** to 0.05:
that number is meant to replace an assumption in research, so an optimistic one is worse
than a conservative one. It refuses to suggest anything from fewer than ten priced fills.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

CONTRACT_MULTIPLIER = Decimal("100")
MIN_FILLS_FOR_SUGGESTION = 10


@dataclass(frozen=True, slots=True)
class Execution:
    contract_key: str
    side: str
    quantity: int
    limit_price: Decimal
    fill_price: Decimal
    fill_qty: int
    intent_ts: datetime
    fill_ts: datetime
    reference: Decimal | None = None

    @property
    def cost_per_contract(self) -> Decimal:
        """Positive is money lost: a buy above its limit or a sell below its limit."""
        if self.side == "BUY":
            return self.fill_price - self.limit_price
        return self.limit_price - self.fill_price

    @property
    def cost_bps(self) -> float:
        if self.limit_price <= 0:
            return 0.0
        return float(self.cost_per_contract / self.limit_price * Decimal("10000"))

    @property
    def cost_usd(self) -> Decimal:
        return self.cost_per_contract * self.fill_qty * CONTRACT_MULTIPLIER

    @property
    def fill_ratio(self) -> float:
        return (self.fill_qty / self.quantity) if self.quantity else 0.0

    @property
    def latency_ms(self) -> float:
        return max(0.0, (self.fill_ts - self.intent_ts).total_seconds() * 1000.0)

    @property
    def multiple_of_reference(self) -> float | None:
        if self.reference is None or self.reference <= 0:
            return None
        return float(self.fill_price / self.reference)


def execution_from_row(row: dict[str, object]) -> Execution:
    """Rebuild an Execution from an `executions` row (prices are stored as TEXT)."""

    def _ts(key: str) -> datetime:
        return datetime.fromisoformat(str(row[key]))

    return Execution(
        contract_key=str(row["contract_key"]),
        side=str(row["side"]),
        quantity=int(str(row["quantity"])),
        limit_price=Decimal(str(row["limit_price"])),
        fill_price=Decimal(str(row["fill_price"])),
        fill_qty=int(str(row["fill_qty"])),
        intent_ts=_ts("intent_ts_utc"),
        fill_ts=_ts("fill_ts_utc"),
    )


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(pct * len(ordered)) - 1))
    return ordered[index]


@dataclass
class FillReport:
    executions: list[Execution] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.executions)

    @property
    def mean_cost_bps(self) -> float:
        return sum(e.cost_bps for e in self.executions) / self.n if self.n else 0.0

    @property
    def p90_cost_bps(self) -> float:
        return _percentile([e.cost_bps for e in self.executions], 0.90)

    @property
    def total_cost_usd(self) -> Decimal:
        return sum((e.cost_usd for e in self.executions), Decimal("0"))

    @property
    def at_or_better_share(self) -> float:
        if not self.n:
            return 0.0
        better = sum(1 for e in self.executions if e.cost_per_contract <= 0)
        return better / self.n * 100.0

    @property
    def partial_share(self) -> float:
        if not self.n:
            return 0.0
        return sum(1 for e in self.executions if e.fill_ratio < 1.0) / self.n * 100.0

    @property
    def latency_p90_ms(self) -> float:
        return _percentile([e.latency_ms for e in self.executions], 0.90)

    def suggested_multiple(self) -> float | None:
        multiples = [m for m in (e.multiple_of_reference for e in self.executions) if m]
        if len(multiples) < MIN_FILLS_FOR_SUGGESTION:
            return None
        p90 = _percentile(multiples, 0.90)
        return math.ceil(p90 * 20) / 20

    def to_dict(self) -> dict[str, object]:
        return {
            "fills": self.n,
            "mean_cost_bps": round(self.mean_cost_bps, 1),
            "p90_cost_bps": round(self.p90_cost_bps, 1),
            "total_cost_usd": str(round(self.total_cost_usd, 2)),
            "at_or_better_pct": round(self.at_or_better_share, 1),
            "partial_pct": round(self.partial_share, 1),
            "latency_p90_ms": round(self.latency_p90_ms, 1),
            "suggested_multiple": self.suggested_multiple(),
        }


def analyse(executions: Iterable[Execution]) -> FillReport:
    return FillReport(executions=list(executions))


def report_from_store(rows: Iterable[dict[str, object]]) -> FillReport:
    return FillReport(executions=[execution_from_row(row) for row in rows])

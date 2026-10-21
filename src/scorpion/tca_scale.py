"""Execution cost at corpus scale: what the fill path costs, by channel and by hour.

The existing TCA layer works on joins it can make for 2 of 18 trades. This one works on the
executions the system itself recorded - every dispatched fill has a limit price, a fill price and a
timestamp - so cost can be measured wherever the executor ran, without waiting for bars.

Three measurements, each with a floor:

* cost in basis points of the limit price, signed so positive means money lost. A fill better than
  the limit carries negative cost, which is real and is not clipped away.
* fill ratio where an intent quantity is known, because a 5-lot that fills 2 has a cost the price
  does not show.
* latency from intent to fill, reported as p50 and p90 rather than a mean - a mean hides the tail
  that actually hurts, and the tail is what the entry-slippage experiment priced.

Every group is withheld below five observations. A cost table that averages two fills is a rumour
with a decimal point, and the slippage experiment showed how much money a wrong execution assumption
is worth.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

D = Decimal
MIN_GROUP = 5


class ScaleError(ValueError):
    """A cost request that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Cost:
    """One execution's quality, from the executor's own record."""

    contract_key: str
    limit_price: D
    fill_price: D
    fill_qty: int
    intent_qty: int
    intent_ts: datetime | None
    fill_ts: datetime | None
    channel: str = ""

    @property
    def cost_bps(self) -> float:
        if self.limit_price <= 0:
            raise ScaleError("bad_limit", str(self.limit_price))
        return float((self.fill_price - self.limit_price) / self.limit_price) * 10_000.0

    @property
    def fill_ratio(self) -> float | None:
        if self.intent_qty <= 0:
            return None
        return self.fill_qty / self.intent_qty

    @property
    def latency_ms(self) -> float | None:
        if self.intent_ts is None or self.fill_ts is None:
            return None
        return (self.fill_ts - self.intent_ts).total_seconds() * 1000.0

    @property
    def hour(self) -> str:
        if self.intent_ts is None:
            return "unknown"
        return f"{self.intent_ts:%H}:00"

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_key": self.contract_key,
            "cost_bps": round(self.cost_bps, 2),
            "fill_qty": self.fill_qty, "intent_qty": self.intent_qty,
            "fill_ratio": None if self.fill_ratio is None else round(self.fill_ratio, 3),
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 1),
            "hour": self.hour,
        }


def _pct(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = p * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _total_cost(costs: Sequence[Cost]) -> float:
    return sum(
        float((c.fill_price - c.limit_price) * c.fill_qty * 100) for c in costs
    )


def _rounded(value: float | None, places: int) -> float | None:
    return None if value is None else round(value, places)


def summarise_costs(costs: Sequence[Cost], *, floor: int = MIN_GROUP) -> dict[str, Any]:
    """The headline numbers for one group, withheld below the floor."""
    if len(costs) < floor:
        return {"n": len(costs), "withheld": f"below the floor of {floor}"}
    bps = [c.cost_bps for c in costs]
    ratios = [c.fill_ratio for c in costs if c.fill_ratio is not None]
    latencies = [c.latency_ms for c in costs if c.latency_ms is not None]
    return {
        "n": len(costs),
        "mean_cost_bps": round(sum(bps) / len(bps), 2),
        "median_cost_bps": round(_pct(bps, 0.5) or 0.0, 2),
        "p90_cost_bps": round(_pct(bps, 0.9) or 0.0, 2),
        "worse_than_limit_pct": round(100.0 * sum(1 for b in bps if b > 0) / len(bps), 1),
        "median_fill_ratio": _rounded(_pct(ratios, 0.5), 3),
        "latency_p50_ms": _rounded(_pct(latencies, 0.5), 1),
        "latency_p90_ms": _rounded(_pct(latencies, 0.9), 1),
        "total_cost_usd": round(_total_cost(costs), 2),
    }


def group_costs(costs: Iterable[Cost], *, by: str = "channel") -> dict[str, dict[str, Any]]:
    """Group by a known axis and withhold thin groups rather than averaging them."""
    buckets: dict[str, list[Cost]] = {}
    for cost in costs:
        key = cost.channel if by == "channel" else cost.hour
        buckets.setdefault(key or "unknown", []).append(cost)
    return {key: summarise_costs(value) for key, value in sorted(buckets.items())}


def from_store(store: Any) -> list[Cost]:
    """Read the executor's own execution rows. Missing quantities become zero, not guesses."""
    rows = store.load_executions()
    out: list[Cost] = []
    for row in rows:
        try:
            limit = D(str(row["limit_price"]))
            fill = D(str(row["fill_price"]))
        except Exception:                     # noqa: BLE001 - a malformed row is data, not a crash
            continue
        if limit <= 0 or fill <= 0:
            continue
        intent_ts = _ts(row.get("intent_ts_utc"))
        fill_ts = _ts(row.get("fill_ts_utc"))
        out.append(Cost(
            contract_key=str(row["contract_key"]),
            limit_price=limit, fill_price=fill,
            fill_qty=int(row.get("fill_qty") or 0),
            intent_qty=int(row.get("fill_qty") or 0),
            intent_ts=intent_ts, fill_ts=fill_ts,
        ))
    return out


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def attach_channels(costs: Sequence[Cost], ledger: Any) -> list[Cost]:
    """Join a channel onto each execution by contract key, leaving unmatched rows unnamed."""
    mapping: dict[str, str] = {}
    for trade in ledger.trades:
        key = f"{trade.ticker}|{trade.side}|{trade.strike}|{trade.day.isoformat()}"
        mapping[key] = trade.channel
    return [
        Cost(
            contract_key=c.contract_key, limit_price=c.limit_price, fill_price=c.fill_price,
            fill_qty=c.fill_qty, intent_qty=c.intent_qty, intent_ts=c.intent_ts,
            fill_ts=c.fill_ts, channel=mapping.get(c.contract_key, "unmatched"),
        )
        for c in costs
    ]


def report(store: Any, ledger: Any | None = None, *, floor: int = MIN_GROUP) -> dict[str, Any]:
    costs = from_store(store)
    if ledger is not None:
        costs = attach_channels(costs, ledger)
    return {
        "executions": len(costs),
        "overall": summarise_costs(costs, floor=floor),
        "by_channel": group_costs(costs, by="channel"),
        "by_hour": group_costs(costs, by="hour"),
        "floor": floor,
        "caveats": [
            "cost is measured against the LIMIT price the executor chose, not against a mid or a "
            "reference quote",
            "a positive basis-point cost is money lost; a negative one is price improvement and is "
            "kept rather than clipped",
            "latency uses the executor's own timestamps, so it measures the paper path until a "
            "real venue fills them",
        ],
    }


def render(payload: Mapping[str, Any]) -> str:
    overall = payload["overall"]
    lines = [f"{payload['executions']} executions"]
    if "withheld" in overall:
        lines.append(f"  overall withheld: {overall['withheld']}")
    else:
        lines.append(
            f"  cost {overall['mean_cost_bps']:+.2f}bps mean  "
            f"{overall['p90_cost_bps']:+.2f}bps p90  "
            f"worse-than-limit {overall['worse_than_limit_pct']}%  "
            f"${overall['total_cost_usd']:+,.2f}"
        )
        lines.append(
            f"  fill ratio {overall['median_fill_ratio']}  latency p50 "
            f"{overall['latency_p50_ms']}ms  p90 {overall['latency_p90_ms']}ms"
        )
    for channel, group in payload["by_channel"].items():
        if "withheld" in group:
            lines.append(f"    {channel:26s} n={group['n']:3d}  withheld")
        else:
            lines.append(
                f"    {channel:26s} n={group['n']:3d}  {group['mean_cost_bps']:+.2f}bps  "
                f"p90 {group['p90_cost_bps']:+.2f}"
            )
    return "\n".join(lines)


def headline(payload: Mapping[str, Any]) -> dict[str, Any]:
    overall = payload["overall"]
    return {
        "executions": payload["executions"],
        "measured": "withheld" not in overall,
        "mean_cost_bps": overall.get("mean_cost_bps"),
        "total_cost_usd": overall.get("total_cost_usd"),
        "groups_withheld": sum(
            1 for group in payload["by_channel"].values() if "withheld" in group
        ),
    }

"""Alert-to-fill drift: how long each hop took, and what it cost.

The stack can already say *what* it decided and *how long the decision took*
(`LatencyBudget`, the `latency` rows, `replay ... worst 0ms`). It could not say the
one thing a latency policy has to be built on: **how long each hop between a Discord
post and a venue fill actually took, and what the delay cost in price.** That is why
the "latency-to-edge threshold" gate in `docs/GEMS.md` was closed: choosing a number
before the distribution existed is choosing a policy by feel.

This module measures the distribution. Every order in the fill log is one row with
four instants and two prices:

    decision   the engine's own `buy`/`sell` row, at the moment the signal was resolved
      |        (the decision, before anything is sent)
    send       the `intent` row, written before the broker call
      |        `decision -> send`  = how long the ladder took to get the order out
    answer     the terminal row (`ack` / `reject` / `transport_unknown`), stamped at the
      |        instant the venue answered and carrying the round trip in `latency_ms`
      |        `send -> answer`    = the venue's own time
    price      the confirmed `ack` price against the submitted limit and, when the post
               stated one, the bid the desk actually posted
               `fill - limit`      = what we paid away from our own order
               `fill - his_bid`    = what we paid over the number the post showed

The instants only exist because `Listener._place` stamps the terminal at the moment the
venue answered. Before this pass it reused the send instant, so a fill log could not tell
a 40ms answer from a 9-second one - the measurement was impossible, not merely unbuilt.

What this module refuses
------------------------
A distribution is not a policy. With fewer than `MIN_SAMPLES` timed fills, the report
prints the numbers it has and **withholds** every suggestion, naming the floor and the
count: three orders cannot price a threshold, and a p99 on three samples is the maximum
wearing a percentile's clothes. The same floor gates the latency-to-edge view, because a
rank correlation on a handful of trades is a coincidence with a decimal point.

It also never assumes an answer. An intent with no terminal is reported as `unresolved`
(counted, with its key), never as a fast order or a lost one; a breaker refusal is
`breaker` with a zero round trip, because it never reached a venue; and a `recovered` /
`not_sent` row is a settlement, not a fill, so it contributes no price.

The report is the input to a policy, not the policy. `suggest_latency_budget_ms` returns a
ceiling derived from the observed p99 (rounded up), and `latency_to_edge` describes what
the data shows without claiming a causal slope. Both are `None` under the floor, and the
reason is printed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .types import Fill

#: The floor from the master law: no model on fewer than twenty observations. It is
#: deliberately the same number the research lanes use, so "under-sampled" means one
#: thing across the stack.
MIN_SAMPLES = 20

#: `entry:pipe:his_bid=1.01:ask=1.05:budget=...` - the posted bid travels inside the
#: intent reason. Parsing it here (rather than adding a field to the log) keeps the row
#: shape every existing reader already understands.
_ENTRY_PRICE_RE = re.compile(r"his_bid=(?P<bid>[0-9]+(?:\.[0-9]+)?)"
                             r":ask=(?P<ask>[0-9]+(?:\.[0-9]+)?)")

DECISION_EVENTS = frozenset({"buy", "sell"})
TERMINAL_EVENTS = frozenset({"ack", "reject", "transport_unknown", "recovered", "not_sent"})


def _percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile. Deterministic, and defined for one sample."""
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _stats(values: Sequence[float]) -> dict[str, Any]:
    """A distribution summary that says how many samples it is summarising."""
    if not values:
        return {"n": 0, "p50": None, "p90": None, "p99": None, "max": None}
    return {
        "n": len(values),
        "p50": round(_percentile(values, 0.50), 3),
        "p90": round(_percentile(values, 0.90), 3),
        "p99": round(_percentile(values, 0.99), 3),
        "max": round(max(values), 3),
    }


@dataclass(frozen=True)
class OrderTiming:
    """One order's journey, from the decision instant to the price it paid."""

    key: str
    action: str
    contract_key: str
    qty: int
    limit: float
    intent_ts: float
    outcome: str                          # ack | reject | transport_unknown | ...
    decision_ts: float | None = None
    answered_ts: float | None = None
    fill_price: float | None = None
    fill_qty: int = 0
    his_bid: float | None = None
    ask_at_entry: float | None = None

    # ── instants ──────────────────────────────────────────────────────────────
    @property
    def decision_ms(self) -> int | None:
        """Decision -> send. None when the engine's own row could not be matched."""
        if self.decision_ts is None:
            return None
        return max(0, round((self.intent_ts - self.decision_ts) * 1000))

    @property
    def round_trip_ms(self) -> int | None:
        """Send -> venue answer. None when the venue never answered."""
        if self.answered_ts is None:
            return None
        return max(0, round((self.answered_ts - self.intent_ts) * 1000))

    @property
    def total_ms(self) -> int | None:
        """Decision -> venue answer: the number a latency ceiling applies to."""
        if self.decision_ts is None or self.answered_ts is None:
            return None
        return max(0, round((self.answered_ts - self.decision_ts) * 1000))

    # ── prices ────────────────────────────────────────────────────────────────
    @property
    def priced(self) -> bool:
        return (self.outcome == "ack" and self.fill_price is not None
                and self.fill_price > 0 and self.limit > 0)

    @property
    def slippage_bps(self) -> float | None:
        """Adverse cost against our own limit. Positive is worse, for both sides."""
        if not self.priced:
            return None
        assert self.fill_price is not None
        edge = (self.fill_price - self.limit) if self.action == "buy" \
            else (self.limit - self.fill_price)
        return round(edge / self.limit * 10_000.0, 2)

    @property
    def through_bid_bps(self) -> float | None:
        """How far through the posted bid our limit was (the pricing rule, measured)."""
        if self.his_bid is None or self.his_bid <= 0:
            return None
        return round((self.limit - self.his_bid) / self.his_bid * 10_000.0, 2)

    @property
    def fill_vs_bid_bps(self) -> float | None:
        """What we actually paid over the number the post showed. Positive is worse."""
        if not self.priced or self.his_bid is None or self.his_bid <= 0:
            return None
        assert self.fill_price is not None
        edge = (self.fill_price - self.his_bid) if self.action == "buy" \
            else (self.his_bid - self.fill_price)
        return round(edge / self.his_bid * 10_000.0, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "action": self.action,
            "contract_key": self.contract_key,
            "qty": self.qty,
            "limit": self.limit,
            "outcome": self.outcome,
            "decision_ms": self.decision_ms,
            "round_trip_ms": self.round_trip_ms,
            "total_ms": self.total_ms,
            "fill_price": self.fill_price,
            "fill_qty": self.fill_qty,
            "his_bid": self.his_bid,
            "slippage_bps": self.slippage_bps,
            "through_bid_bps": self.through_bid_bps,
            "fill_vs_bid_bps": self.fill_vs_bid_bps,
        }


def _entry_prices(reason: str) -> tuple[float | None, float | None]:
    match = _ENTRY_PRICE_RE.search(reason)
    if match is None:
        return None, None
    return float(match.group("bid")), float(match.group("ask"))


def build(rows: Iterable[Fill]) -> list[OrderTiming]:
    """Join the fill log into one timing per order. Pure, total, never raises.

    The join is by the idempotency key the intent and its terminal already share (the
    ledger's rule, reused rather than reinvented). The decision row is matched by
    contract and order: the engine writes one `buy`/`sell` row per intent, immediately
    before `_place` writes the intent, so the k-th intent for a contract belongs to the
    k-th decision row for it. If that row is missing - a pre-ledger log, or a test that
    recorded an intent directly - the decision instant is `None` and the order is counted
    as `no_decision` rather than given a fabricated start time.
    """
    ordered = sorted(rows, key=lambda fill: fill.seq)
    decisions: dict[str, list[Fill]] = {}
    for fill in ordered:
        if fill.event in DECISION_EVENTS:
            decisions.setdefault(fill.contract_key, []).append(fill)
    consumed: dict[str, int] = {}

    terminals: dict[str, Fill] = {}
    for fill in ordered:
        if fill.event in TERMINAL_EVENTS:
            terminals[fill.note] = fill          # the LAST answer is the state

    timings: list[OrderTiming] = []
    for fill in ordered:
        if fill.event != "intent":
            continue
        key = fill.note
        rows_for_contract = decisions.get(fill.contract_key, [])
        index = consumed.get(fill.contract_key, 0)
        decision = None
        if index < len(rows_for_contract):
            candidate = rows_for_contract[index]
            if candidate.ts <= fill.ts + 1e-6:
                decision = candidate
                consumed[fill.contract_key] = index + 1
        terminal = terminals.get(key)
        outcome = "unresolved"
        answered: float | None = None
        fill_price = None
        fill_qty = 0
        if terminal is not None:
            outcome = terminal.event
            answered = terminal.ts
            if terminal.event == "ack":
                fill_price = terminal.price
                fill_qty = terminal.qty
            elif terminal.event == "reject" and terminal.reason == "breaker_open":
                outcome = "breaker"              # a local refusal, not a venue answer
                answered = None
        his_bid, ask = _entry_prices(fill.reason)
        # The decision row knows the side; the key is the fallback for a log written by a
        # test or an older build (a close uses `:close:` / `:trim:`, never `:buy:`).
        action = (decision.event if decision is not None
                  else ("buy" if ":buy:" in key else "sell"))
        timings.append(OrderTiming(
            key=key, action=action,
            contract_key=fill.contract_key, qty=fill.qty, limit=float(fill.price),
            intent_ts=fill.ts, outcome=outcome,
            decision_ts=(decision.ts if decision is not None else None),
            answered_ts=answered, fill_price=fill_price, fill_qty=fill_qty,
            his_bid=his_bid, ask_at_entry=ask,
        ))
    return timings


@dataclass
class DriftReport:
    """The measured distribution, and every suggestion the sample cannot support."""

    timings: list[OrderTiming] = field(default_factory=list)
    floor: int = MIN_SAMPLES

    # ── counts ────────────────────────────────────────────────────────────────
    @property
    def orders(self) -> int:
        return len(self.timings)

    def _outcome(self, name: str) -> list[OrderTiming]:
        return [row for row in self.timings if row.outcome == name]

    @property
    def answered(self) -> list[OrderTiming]:
        """Orders the venue answered (a fill or a refusal), not local refusals."""
        return [row for row in self.timings
                if row.outcome in {"ack", "reject", "transport_unknown"}]

    @property
    def priced(self) -> list[OrderTiming]:
        return [row for row in self.timings if row.priced]

    @property
    def timed(self) -> list[OrderTiming]:
        """Answers with a measurable round trip: the population a budget applies to."""
        return [row for row in self.timings if row.round_trip_ms is not None]

    @property
    def unresolved(self) -> list[OrderTiming]:
        return self._outcome("unresolved")

    @property
    def no_decision(self) -> list[OrderTiming]:
        return [row for row in self.timings if row.decision_ts is None]

    # ── distributions ─────────────────────────────────────────────────────────
    def _ms(self, attr: str, rows: Sequence[OrderTiming] | None = None) -> dict[str, Any]:
        source = self.timings if rows is None else rows
        values = [value for row in source if (value := getattr(row, attr)) is not None]
        return _stats([float(value) for value in values])

    @property
    def decision(self) -> dict[str, Any]:
        return self._ms("decision_ms")

    @property
    def round_trip(self) -> dict[str, Any]:
        return self._ms("round_trip_ms", self.timed)

    @property
    def total(self) -> dict[str, Any]:
        return self._ms("total_ms", self.timed)

    @property
    def cost_bps(self) -> dict[str, Any]:
        values = [row.slippage_bps for row in self.priced
                  if row.slippage_bps is not None]
        return _stats([float(value) for value in values])

    @property
    def through_bid_bps(self) -> dict[str, Any]:
        values = [row.through_bid_bps for row in self.timings
                  if row.through_bid_bps is not None]
        return _stats([float(value) for value in values])

    @property
    def paid_vs_bid_bps(self) -> dict[str, Any]:
        values = [row.fill_vs_bid_bps for row in self.priced
                  if row.fill_vs_bid_bps is not None]
        return _stats([float(value) for value in values])

    # ── suggestions the sample may not support ────────────────────────────────
    def _floor_reason(self, n: int, what: str) -> str:
        return (f"REFUSED - {n} {what} < floor {self.floor}: a threshold on this "
                f"sample is a number, not a policy")

    def withheld(self) -> list[str]:
        """Every suggestion this report declines to make, with the reason."""
        reasons: list[str] = []
        if len(self.priced) < self.floor:
            reasons.append(self._floor_reason(len(self.priced), "priced order(s)"))
        if len(self.timed) < self.floor:
            reasons.append(self._floor_reason(len(self.timed), "timed answer(s)"))
        if self.unresolved:
            reasons.append(
                f"{len(self.unresolved)} order(s) have no venue answer - the venue's time "
                f"for them is unknown, not fast")
        if self.no_decision:
            reasons.append(
                f"{len(self.no_decision)} intent(s) have no decision row - the start of "
                f"the journey is unknown, not zero")
        return reasons

    def suggest_latency_budget_ms(self) -> int | None:
        """A ceiling from the observed p99, rounded up to 250ms. None under the floor.

        This is a *budget* (a ceiling the path must stay under), not a latency-to-edge
        threshold. The difference matters: a budget can be derived from the latency
        distribution alone, while an edge threshold needs the price outcome joined to the
        delay - see `latency_to_edge`, which is much stricter.
        """
        if len(self.timed) < self.floor:
            return None
        p99 = self.total.get("p99")
        if p99 is None:
            return None
        return int(math.ceil(float(p99) / 250.0) * 250)

    def latency_to_edge(self) -> dict[str, Any] | None:
        """What the data shows about delay and price - or None when it shows too little.

        Requires `floor` orders that are BOTH priced and timed: the delay and the money
        have to be measured on the same order, or the join is across two populations.
        The output is descriptive (a rank correlation and the median outcome each side of
        the median delay), explicitly not a fitted slope: at this sample size a slope
        would be arithmetic with a confidence interval doing no work.
        """
        pairs = [row for row in self.timed
                 if row.priced and row.fill_vs_bid_bps is not None]
        if len(pairs) < self.floor:
            return None
        median_ms = float(_percentile([float(row.total_ms or 0) for row in pairs], 0.50))
        fast = [value for row in pairs if (row.total_ms or 0) <= median_ms
                and (value := row.fill_vs_bid_bps) is not None]
        slow = [value for row in pairs if (row.total_ms or 0) > median_ms
                and (value := row.fill_vs_bid_bps) is not None]
        correlation = _spearman(
            [float(row.total_ms or 0) for row in pairs],
            [float(row.fill_vs_bid_bps or 0.0) for row in pairs],
        )
        return {
            "n": len(pairs),
            "median_total_ms": round(median_ms, 1),
            "fast_n": len(fast),
            "slow_n": len(slow),
            "fast_median_paid_bps": round(_percentile([float(v) for v in fast], 0.50), 2)
            if fast else None,
            "slow_median_paid_bps": round(_percentile([float(v) for v in slow], 0.50), 2)
            if slow else None,
            "spearman_ms_vs_paid": round(correlation, 3) if correlation is not None else None,
            "claim": "descriptive only; this is not a causal slope",
        }

    # ── output ────────────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "orders": self.orders,
            "priced": len(self.priced),
            "timed": len(self.timed),
            "unresolved": [row.to_dict() for row in self.unresolved],
            "counts": {
                "ack": len(self._outcome("ack")),
                "reject": len(self._outcome("reject")),
                "transport_unknown": len(self._outcome("transport_unknown")),
                "breaker": len(self._outcome("breaker")),
                "recovered": len(self._outcome("recovered")),
                "not_sent": len(self._outcome("not_sent")),
                "unresolved": len(self.unresolved),
                "no_decision": len(self.no_decision),
            },
            "decision_ms": self.decision,
            "round_trip_ms": self.round_trip,
            "total_ms": self.total,
            "cost_bps": self.cost_bps,
            "through_bid_bps": self.through_bid_bps,
            "paid_vs_bid_bps": self.paid_vs_bid_bps,
            "floor": self.floor,
            "withheld": self.withheld(),
            "suggested_latency_budget_ms": self.suggest_latency_budget_ms(),
            "latency_to_edge": self.latency_to_edge(),
        }

    def to_text(self) -> str:
        lines = [
            f"drift  orders={self.orders}  priced={len(self.priced)}  "
            f"timed={len(self.timed)}  unanswered={len(self.unresolved)}  "
            f"floor={self.floor}",
        ]
        for label, stats in (("decision -> send", self.decision),
                             ("send -> answer", self.round_trip),
                             ("decision -> answer", self.total)):
            lines.append(_stats_line(label, stats, "ms"))
        for label, stats in (("cost vs limit", self.cost_bps),
                             ("posted bid -> limit", self.through_bid_bps),
                             ("paid vs posted bid", self.paid_vs_bid_bps)):
            lines.append(_stats_line(label, stats, "bps"))
        budget = self.suggest_latency_budget_ms()
        if budget is None:
            lines.append(f"latency budget     REFUSED - {len(self.timed)} timed answer(s) "
                         f"< floor {self.floor}")
        else:
            lines.append(f"latency budget     {budget}ms (p99 of decision->answer, "
                         f"rounded up; a ceiling, not an edge claim)")
        edge = self.latency_to_edge()
        if edge is None:
            pairs = sum(1 for row in self.timed
                        if row.priced and row.fill_vs_bid_bps is not None)
            lines.append(f"latency -> edge    REFUSED - {pairs} priced+timed order(s) "
                         f"< floor {self.floor}")
        else:
            lines.append(
                f"latency -> edge    n={edge['n']}  median {edge['median_total_ms']:g}ms  "
                f"paid<=median {edge['fast_median_paid_bps']}bps  "
                f"paid>median {edge['slow_median_paid_bps']}bps  "
                f"spearman {edge['spearman_ms_vs_paid']}  ({edge['claim']})")
        for reason in self.withheld():
            lines.append(f"  withheld         {reason}")
        for row in self.unresolved:
            lines.append(f"  unresolved       {row.action:<4} {row.qty}x "
                         f"{row.contract_key:<28} {row.key} (no venue answer)")
        return "\n".join(lines)


def _stats_line(label: str, stats: dict[str, Any], unit: str) -> str:
    if not stats["n"]:
        return f"{label:<19} (no samples)"
    return (f"{label:<19} p50 {stats['p50']:>8g}{unit}  p90 {stats['p90']:>8g}{unit}  "
            f"p99 {stats['p99']:>8g}{unit}  max {stats['max']:>8g}{unit}  "
            f"(n={stats['n']})")


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties do not invent an ordering."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        stop = index
        while stop + 1 < len(order) and values[order[stop + 1]] == values[order[index]]:
            stop += 1
        average = (index + stop) / 2.0 + 1.0
        for position in range(index, stop + 1):
            ranks[order[position]] = average
        index = stop + 1
    return ranks


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation without a dependency; None when it is undefined.

    Undefined means one side never varies (every order had the same delay, or the same
    price), which is not a correlation of zero - it is no information.
    """
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    if len(set(rx)) < 2 or len(set(ry)) < 2:
        return None
    mean_x = sum(rx) / len(rx)
    mean_y = sum(ry) / len(ry)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(rx, ry, strict=True))
    denom_x = math.sqrt(sum((x - mean_x) ** 2 for x in rx))
    denom_y = math.sqrt(sum((y - mean_y) ** 2 for y in ry))
    if denom_x == 0 or denom_y == 0:
        return None
    return numerator / (denom_x * denom_y)


def report(rows: Iterable[Fill], *, floor: int = MIN_SAMPLES) -> DriftReport:
    """Build the report from fill-log rows. The CLI's whole body."""
    if floor < 1:
        raise ValueError(f"floor must be at least 1, not {floor}")
    return DriftReport(timings=build(rows), floor=floor)

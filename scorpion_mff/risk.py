"""Pre-trade risk budget: the loss you have not taken yet.

The governor in `governor.py` is a *realised* risk layer. It reacts to money already
lost. That leaves a hole that a two-position book makes wide:

    daily loss limit  = $3,000 (2R)
    two high-confidence entries at $3,000 premium each
    both expire worthless  ->  -$6,000 realised, twice the limit

The stop cannot fire before the loss exists. This module closes the hole by pricing
the *worst case before the order*: for a long option, the maximum loss is the premium
paid, so the book's worst-case day is `realised_today - open_premium - new_premium`.
A candidate is admitted only if that number stays inside the same daily limit the
governor enforces after the fact.

Three budgets, all reduce-only:

* **Total premium** - the whole book cannot risk more than this.
* **Underlying** - one ticker cannot become the book (two QQQ strikes are one bet).
* **Cluster** - index ETFs and mega-cap tech move together; a book of QQQ + SPY +
  NVDA calls is one directional bet with three tickets.

The cluster map is declared, not inferred. An unknown symbol is its own cluster:
guessing a correlation is how a "diversified" book becomes a single position. A
cluster entry is a set of symbols treated as one exposure; the first matching set
wins, and a symbol may appear in only one set (validated).

Everything here is pure. No clock, no I/O in `admit`, no opinion about direction:
long options only, premium is the risk, and the answer is a contract count that is
never larger than the one requested.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .types import Position

OPTION_MULTIPLIER = 100.0
DEFAULT_CLUSTERS: tuple[frozenset[str], ...] = (
    frozenset({"SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "RSP"}),
    frozenset({"NVDA", "AMD", "AVGO", "MU", "TSM", "SMCI", "ARM", "INTC", "QCOM", "MRVL", "ASML"}),
    frozenset({"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NFLX", "TSLA",
               "ORCL", "CRM", "ADBE", "NOW", "IBM"}),
    frozenset({"COIN", "MSTR", "HOOD", "SOFI", "IBIT", "MARA", "RIOT", "CLSK"}),
)


class RiskBudgetError(ValueError):
    """The budget cannot be trusted. Refuse to arm rather than guess a limit."""


@dataclass(frozen=True)
class Admission:
    """How many contracts the budget allows, and every cap that bound."""

    qty: int
    reasons: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.qty > 0

    @property
    def capped(self) -> bool:
        return bool(self.reasons)

    @property
    def reason_text(self) -> str:
        return "+".join(self.reasons) if self.reasons else "within_budget"

    def to_dict(self) -> dict:
        return {"qty": self.qty, "reasons": list(self.reasons), "accepted": self.accepted}


def premium_at_risk(position: Position) -> float:
    """Maximum loss on a long option position: the premium paid.

    Cost basis, not current mark. A position that is up cannot lose more than it
    cost, so this is conservative at every price and exact at zero.
    """
    if position.contracts <= 0 or position.avg_price <= 0:
        return 0.0
    return position.contracts * position.avg_price * OPTION_MULTIPLIER


@dataclass(frozen=True)
class RiskBudget:
    """A versioned, reduce-only set of pre-trade limits. `None` means "no cap"."""

    version: int = 1
    max_total_premium_usd: float | None = None
    max_underlying_premium_usd: float | None = None
    max_cluster_premium_usd: float | None = None
    daily_loss_limit_usd: float | None = None
    clusters: tuple[frozenset[str], ...] = DEFAULT_CLUSTERS

    def __post_init__(self) -> None:
        if self.version != 1:
            raise RiskBudgetError(f"version: expected 1, got {self.version!r}")
        for name in ("max_total_premium_usd", "max_underlying_premium_usd",
                     "max_cluster_premium_usd", "daily_loss_limit_usd"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RiskBudgetError(f"{name}: must be a number or null")
            if not math.isfinite(float(value)) or value <= 0:
                raise RiskBudgetError(f"{name}: must be finite and positive")
        seen: dict[str, int] = {}
        for index, group in enumerate(self.clusters):
            if not group:
                raise RiskBudgetError(f"clusters[{index}]: empty cluster")
            for symbol in group:
                if not isinstance(symbol, str) or symbol.strip() != symbol or not symbol:
                    raise RiskBudgetError(f"clusters[{index}]: invalid symbol {symbol!r}")
                key = symbol.upper()
                if key in seen:
                    raise RiskBudgetError(
                        f"clusters: {key} appears in clusters[{seen[key]}] and "
                        f"clusters[{index}]; one symbol, one cluster")
                seen[key] = index

    # ── reading ───────────────────────────────────────────────────────────────
    def cluster_for(self, symbol: str) -> str:
        """The declared cluster, or the symbol itself when it has not been mapped."""
        key = (symbol or "").strip().upper()
        if not key:
            return ""
        for index, group in enumerate(self.clusters):
            if key in group:
                return f"cluster:{index}:{'/'.join(sorted(group))}"
        return f"symbol:{key}"

    def caps(self) -> dict[str, float]:
        return {name: float(value) for name in
                ("max_total_premium_usd", "max_underlying_premium_usd",
                 "max_cluster_premium_usd")
                if (value := getattr(self, name)) is not None}

    # ── the decision ──────────────────────────────────────────────────────────
    def admit(self, book: Iterable[Position], *, ticker: str, qty: int,
              limit: float, realized_today_usd: float = 0.0) -> Admission:
        """The largest contract count that keeps every limit intact.

        `book` is the live book BEFORE the candidate. `qty` is what the locked clip
        and the governor already allowed; the return value is never larger.
        """
        if qty <= 0:
            return Admission(0, ("invalid_qty",))
        if not math.isfinite(limit) or limit <= 0:
            return Admission(0, ("invalid_limit",))
        symbol = (ticker or "").strip().upper()
        if not symbol:
            return Admission(0, ("invalid_ticker",))

        open_positions = [p for p in book if p.is_open]
        existing_total = sum(premium_at_risk(p) for p in open_positions)
        existing_underlying = sum(premium_at_risk(p) for p in open_positions
                                  if p.ticker.upper() == symbol)
        cluster = self.cluster_for(symbol)
        existing_cluster = sum(premium_at_risk(p) for p in open_positions
                               if self.cluster_for(p.ticker) == cluster)

        # Headroom in dollars, then converted to whole contracts at this limit.
        headroom: list[tuple[str, float]] = []
        if self.max_total_premium_usd is not None:
            headroom.append(("total_premium", self.max_total_premium_usd - existing_total))
        if self.max_underlying_premium_usd is not None:
            headroom.append(("underlying_premium",
                             self.max_underlying_premium_usd - existing_underlying))
        if self.max_cluster_premium_usd is not None:
            headroom.append(("cluster_premium",
                             self.max_cluster_premium_usd - existing_cluster))
        if self.daily_loss_limit_usd is not None:
            # Worst case after this order: realised - open premium - new premium.
            # Keep that at or above -limit, so new premium <= limit + realised - open.
            headroom.append(("daily_worst_case",
                             self.daily_loss_limit_usd + realized_today_usd - existing_total))

        allowed = qty
        reasons: list[str] = []
        per_contract = limit * OPTION_MULTIPLIER
        for name, dollars in headroom:
            if dollars <= 0:
                cap = 0
            else:
                cap = int(dollars // per_contract)
            # Report every cap that binds, not only the smallest: an operator
            # reading "qty 5, daily_worst_case" needs to know the cluster cap also
            # bound at 6 and the total at 10.
            if cap < qty:
                reasons.append(name)
            allowed = min(allowed, cap)
        return Admission(max(0, min(qty, allowed)), tuple(reasons))

    # ── persistence ───────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "max_total_premium_usd": self.max_total_premium_usd,
            "max_underlying_premium_usd": self.max_underlying_premium_usd,
            "max_cluster_premium_usd": self.max_cluster_premium_usd,
            "daily_loss_limit_usd": self.daily_loss_limit_usd,
            "clusters": [sorted(group) for group in self.clusters],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RiskBudget:
        if not isinstance(payload, Mapping):
            raise RiskBudgetError("risk budget must be an object")
        known = {"version", "max_total_premium_usd", "max_underlying_premium_usd",
                 "max_cluster_premium_usd", "daily_loss_limit_usd", "clusters"}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise RiskBudgetError(f"unknown key(s): {', '.join(unknown)}")

        clusters_raw = payload.get("clusters", DEFAULT_CLUSTERS)
        if not isinstance(clusters_raw, (list, tuple)):
            raise RiskBudgetError("clusters: must be an array of symbol arrays")
        clusters: list[frozenset[str]] = []
        for index, group in enumerate(clusters_raw):
            if not isinstance(group, (list, tuple)) or not group:
                raise RiskBudgetError(f"clusters[{index}]: must be a non-empty array")
            symbols = []
            for item in group:
                if not isinstance(item, str) or not item.strip():
                    raise RiskBudgetError(f"clusters[{index}]: symbols must be strings")
                symbols.append(item.strip().upper())
            clusters.append(frozenset(symbols))

        version = payload.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int):
            raise RiskBudgetError("version: must be an integer")

        return cls(
            version=version,
            max_total_premium_usd=_optional_number(payload, "max_total_premium_usd"),
            max_underlying_premium_usd=_optional_number(payload, "max_underlying_premium_usd"),
            max_cluster_premium_usd=_optional_number(payload, "max_cluster_premium_usd"),
            daily_loss_limit_usd=_optional_number(payload, "daily_loss_limit_usd"),
            clusters=tuple(clusters),
        )

    @classmethod
    def load(cls, path: str | Path, *, missing_ok: bool = True) -> RiskBudget | None:
        p = Path(path)
        try:
            raw = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            if missing_ok:
                return None
            raise RiskBudgetError(f"risk budget not found: {p}") from None
        except OSError as exc:
            raise RiskBudgetError(f"cannot read risk budget at {p}: {exc}") from None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RiskBudgetError(f"risk budget at {p} is not JSON: {exc}") from None
        return cls.from_dict(payload)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)

    def report(self, book: Iterable[Position], *, realized_today_usd: float = 0.0) -> dict:
        """Current usage against every cap - the operator's pre-open check."""
        open_positions = [p for p in book if p.is_open]
        total = sum(premium_at_risk(p) for p in open_positions)
        by_underlying: dict[str, float] = {}
        by_cluster: dict[str, float] = {}
        for p in open_positions:
            symbol = p.ticker.upper()
            by_underlying[symbol] = by_underlying.get(symbol, 0.0) + premium_at_risk(p)
            cluster = self.cluster_for(symbol)
            by_cluster[cluster] = by_cluster.get(cluster, 0.0) + premium_at_risk(p)
        worst_case = realized_today_usd - total
        return {
            "open_positions": len(open_positions),
            "premium_at_risk_usd": round(total, 2),
            "realized_today_usd": round(realized_today_usd, 2),
            "worst_case_day_usd": round(worst_case, 2),
            "daily_loss_limit_usd": self.daily_loss_limit_usd,
            "daily_headroom_usd": (round(self.daily_loss_limit_usd + worst_case, 2)
                                   if self.daily_loss_limit_usd is not None else None),
            "total_headroom_usd": (round(self.max_total_premium_usd - total, 2)
                                   if self.max_total_premium_usd is not None else None),
            "by_underlying": {k: round(v, 2) for k, v in sorted(by_underlying.items())},
            "by_cluster": {k: round(v, 2) for k, v in sorted(by_cluster.items())},
        }


def _optional_number(payload: Mapping[str, object], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RiskBudgetError(f"{key}: must be a number or null")
    return float(value)

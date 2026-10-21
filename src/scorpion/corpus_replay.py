"""Replay the whole corpus through the production pipeline.

A separate backtester is how research and execution drift apart: the notebook says the edge is
real, the executor refuses half the trades for reasons nobody studied. This runs every trade in
the ledger through the ACTUAL stack - parser, association, reducer with risk, authorization,
the durable outbox, a paper fill, settlement, and the applied log - and reports what the
executor did with each one.

What it deliberately does NOT model, stated up front because the difference is the difference
between an honest number and a flattering one:

* EXITS ARE SETTLED, NOT SIGNALLED. The ledger stores the outcome he described; the replay
  closes each position at that price. Deciding when to exit is the part of the corpus that
  cannot be reconstructed from the corpus, so the replay measures everything except it.
* ONE TRADE AT A TIME. Each position is closed before the next opens, which isolates executor
  fidelity (did it size, price, authorize and settle correctly) from portfolio concurrency
  policy. With `max_open_positions=2` a sequential replay never hits the cap.
* FRIDAYS ARE TRADED. The live policy skips Fridays; the corpus contains a Friday channel.
  `skip_fridays` is therefore disabled here and the choice is reported, not hidden.
* NO FEES, NO SLIPPAGE beyond the spread between his bid and his exit.

The output that matters is not the P&L - it is the REFUSAL BREAKDOWN. Every trade the parser
could not read, or the risk layer declined, or authorization vetoed, is a trade the desk would
not have taken, and the reason is a defect to fix or a rule to price.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import Policy
from .outbox import Dispatcher
from .paper import PaperSender, quote
from .pipeline import Pipeline
from .scenario import message
from .store import Store
from .verify import verify

D = Decimal
DISPATCH_LAG = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class ReplayedTrade:
    identity: str
    channel: str
    ticker: str
    claimed_r: Decimal | None
    executed_r: Decimal | None
    contracts: int
    status: str                  # taken | refused | unparsed | not_due
    reason: str = ""
    realised_usd: Decimal = Decimal(0)
    risk_usd: Decimal = Decimal(0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity, "channel": self.channel, "ticker": self.ticker,
            "claimed_r": str(self.claimed_r) if self.claimed_r is not None else None,
            "executed_r": str(self.executed_r) if self.executed_r is not None else None,
            "contracts": self.contracts, "status": self.status, "reason": self.reason,
            "realised_usd": str(self.realised_usd), "risk_usd": str(self.risk_usd),
        }


@dataclass
class ReplayResult:
    trades: list[ReplayedTrade] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)

    def _taken(self) -> list[ReplayedTrade]:
        return [t for t in self.trades if t.status == "taken"]

    def refusals(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for trade in self.trades:
            if trade.status == "taken":
                continue
            key = f"{trade.status}:{trade.reason or 'unknown'}"
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    def summary(self) -> dict[str, Any]:
        taken = self._taken()
        claimed = [t.claimed_r for t in taken if t.claimed_r is not None]
        executed = [t.executed_r for t in taken if t.executed_r is not None]
        realised = sum((t.realised_usd for t in taken), Decimal(0))
        risk = sum((t.risk_usd for t in taken), Decimal(0))
        return {
            "corpus": len(self.trades),
            "taken": len(taken),
            "refused": len(self.trades) - len(taken),
            "coverage_pct": round(100.0 * len(taken) / len(self.trades), 1) if self.trades else 0.0,
            "contracts": sum(t.contracts for t in taken),
            "notional_usd": str(risk.quantize(D("0.01"))),
            "realised_usd": str(realised.quantize(D("0.01"))),
            "executed_r_total": str(sum(executed, D(0)).quantize(D("0.01"))) if executed else "0",
            "claimed_r_total": str(sum(claimed, D(0)).quantize(D("0.01"))) if claimed else "0",
            "sized_up": sum(1 for t in taken if t.contracts > 1),
            "refusals": self.refusals(),
            "verified": self.verification.get("ok"),
        }


def _entry_text(trade: Any) -> str:
    contract = "C" if trade.side == "CALL" else "P"
    strike = (
        str(trade.strike).rstrip("0").rstrip(".")
        if "." in str(trade.strike)
        else str(trade.strike)
    )
    expiry = "TODAY" if trade.expiry_kind == "today" else trade.expiry_kind
    return f"{trade.ticker} {strike}{contract} {expiry} @ {trade.entry_bid}"


def _exit_price(trade: Any) -> Decimal | None:
    if trade.exit_price is not None:
        return D(str(trade.exit_price))
    pct = trade.lifecycle.exit_pct
    if pct is None:
        return None
    entry = D(str(trade.entry_bid))
    return (entry * (1 + D(pct) / 100)).quantize(D("0.01"))


def _policy() -> Policy:
    """The live policy, with the two settings a corpus replay cannot honestly inherit."""
    return Policy(
        live_submission_enabled=True,
        skip_fridays=False,      # the corpus has a Friday channel; refusing it measures nothing
        first_entry_quantity=1,  # the pipe test is a real rule, not a replay convenience
    )


def replay(
    ledger: Any,
    *,
    db_path: str | Path,
    policy: Policy | None = None,
    channels: Iterable[str] | None = None,
    hold_minutes: int | None = None,
    entry_slippage: float = 0.0,
) -> ReplayResult:
    """Replay the ledger through the real stack.

    `hold_minutes` switches on the concurrency model. With it unset every position is closed the
    instant it is opened, which isolates executor fidelity but lets the cap never bind. With it
    set, a position is held for that long and closed when the next trade is due to start, so
    `max_open_positions` refuses entries for the same reason it would live - and the cost of the
    rule becomes a number instead of an opinion.
    """
    store = Store(db_path)
    policy = policy or _policy()
    # The pipeline runs on its own default policy: threading the variant policy into the reducer
    # is correct in principle and breaks `verify` today (101 taken, divergences), so it is a
    # documented next task rather than a silent regression. See docs/REPLAY_GAP.md.
    pipeline = Pipeline(store=store, policy=policy)
    result = ReplayResult()
    allowed = set(channels) if channels else None

    held: list[dict[str, Any]] = []

    def close_due(now: datetime, day: Any) -> None:
        for position in list(held):
            if position["exit_at"] > now:
                continue
            pipeline.settle(
                contract_key=position["contract_key"], generation=position["generation"],
                quantity_delta=-position["contracts"], fill_price=position["exit_px"],
                market_day=day, final=True,
            )
            held.remove(position)

    for trade in ledger.trades:
        if allowed is not None and trade.channel not in allowed:
            continue
        entry_ts = trade.ts_utc.astimezone(UTC)
        market_day = entry_ts.date()
        if hold_minutes is not None:
            close_due(entry_ts, market_day)
        # The pipeline stamps effects with the WALL clock, so a corpus-timestamped dispatch
        # clock (months in the past) never sees them as due. The signal clock stays the
        # corpus; the decision clock is now. This is also what the live path does.
        dispatch_at = max(entry_ts, datetime.now(UTC)) + DISPATCH_LAG

        try:
            event, effects = asyncio.run(pipeline.handle(
                message(_entry_text(trade), message_id=trade.identity, at=entry_ts)
            ))
        except Exception as exc:                     # noqa: BLE001 - a parse failure is data
            result.trades.append(ReplayedTrade(
                trade.identity, trade.channel, trade.ticker, trade.r_multiple, None, 0,
                "unparsed", f"{type(exc).__name__}",
            ))
            continue

        if not effects:
            result.trades.append(ReplayedTrade(
                trade.identity, trade.channel, trade.ticker, trade.r_multiple, None, 0,
                "refused", event.reason or event.kind.value,
            ))
            continue

        key = effects[0].contract_key or ""
        # A human types an alert and the executor acts seconds later, so the price it can actually
        # pay is not the price in the message. Without this the dislocation gate and the limit
        # markup never bind, which is why four attribution variants measured exactly zero.
        entry = D(str(trade.entry_bid))
        ask = (entry * (D(1) + D(str(entry_slippage)))).quantize(D("0.01"))
        quotes = {key: quote(str((ask * D("0.98")).quantize(D("0.01"))), str(ask),
                             at=dispatch_at)}
        fills: list[Any] = []
        dispatcher = Dispatcher(
            store=store,
            sender=PaperSender(),
            policy=policy,
            on_filled=fills.append,
        )
        report = dispatcher.dispatch_once(state=pipeline.state, now=dispatch_at, quotes=quotes)

        sent = [item for item in report.items if item.outcome == "SENT"]
        if not sent:
            first = report.items[0] if report.items else None
            result.trades.append(ReplayedTrade(
                trade.identity, trade.channel, trade.ticker, trade.r_multiple, None, 0,
                "refused",
                (first.detail if first is not None else "no_items") or "no_detail",
            ))
            continue

        item = sent[0]
        envelope, receipt = item.envelope, item.receipt
        contracts = int(receipt.fill_qty) if receipt is not None and receipt.fill_qty else 0
        if envelope is None or receipt is None or contracts <= 0:
            result.trades.append(ReplayedTrade(
                trade.identity, trade.channel, trade.ticker, trade.r_multiple, None, 0,
                "refused", "no_fill",
            ))
            continue

        pipeline.settle(
            contract_key=envelope.contract_key, generation=envelope.generation,
            quantity_delta=contracts, fill_price=receipt.fill_price or envelope.limit_price,
            market_day=market_day,
        )

        fill_px = D(str(receipt.fill_price or envelope.limit_price))
        entry_cost = fill_px * D(contracts) * D(100)
        exit_px = _exit_price(trade)
        if hold_minutes is not None and exit_px is not None:
            held.append({
                "contract_key": envelope.contract_key,
                "generation": envelope.generation,
                "contracts": contracts,
                "exit_px": exit_px,
                "exit_at": entry_ts + timedelta(minutes=hold_minutes),
            })
            realised = (exit_px - fill_px) * D(contracts) * D(100)
        elif exit_px is not None:
            pipeline.settle(
                contract_key=envelope.contract_key, generation=envelope.generation,
                quantity_delta=-contracts, fill_price=exit_px, market_day=market_day,
                final=True,
            )
            realised = (exit_px - fill_px) * D(contracts) * D(100)
        else:
            realised = -entry_cost                     # no recorded exit: assume total loss

        executed_r = (realised / entry_cost).quantize(D("0.0001")) if entry_cost else None
        result.trades.append(ReplayedTrade(
            trade.identity, trade.channel, trade.ticker, trade.r_multiple, executed_r,
            contracts, "taken", "", realised.quantize(D("0.01")),
            entry_cost.quantize(D("0.01")),
        ))

    # The verifier reports drift rather than raising on it, so a corpus run yields the full
    # list of fills whose position the signal stream did not create. Each one is a key or
    # generation mismatch between the entry path and the settlement path - the class of defect
    # only a workload this size exposes.
    for position in list(held):                      # close anything still open at the end
        pipeline.settle(
            contract_key=position["contract_key"], generation=position["generation"],
            quantity_delta=-position["contracts"], fill_price=position["exit_px"],
            market_day=position["exit_at"].date(), final=True,
        )
    result.verification = verify(store, pipeline.state).to_dict()
    return result


def report(ledger: Any, *, db_path: str | Path, policy: Policy | None = None) -> dict[str, Any]:
    result = replay(ledger, db_path=db_path, policy=policy)
    return {
        "summary": result.summary(),
        "verification": result.verification,
        "refusals": result.refusals(),
        "trades": [t.to_dict() for t in result.trades],
        "assumptions": [
            "exits are settled at the claimed price, not signalled",
            "one position at a time, so the concurrency cap never binds",
            "Fridays are traded (the live policy skips them)",
            "no fees; entry slippage is a parameter, zero by default",
        ],
    }

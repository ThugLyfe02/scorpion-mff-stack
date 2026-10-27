"""Replay a recorded tape through the real pipeline, offline.

This is the proof harness: it runs the SAME parser, engine, pricing and sizing
the live loop uses, over a recorded tape, and reports what would have happened -
trades, sizes, limits, skips with reasons, and the realised P&L.

The 9/8 tape (tapes/sep8.json) is rebuilt from live/misses/2026-09-08.json, the
post-mortem of the two alerts the agent-shaped path lost.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .broker import PaperBroker, QuoteScript
from .engine import Engine, EngineConfig
from .fills import FillLog
from .health import Heartbeat, LatencyBudget
from .killswitch import KillSwitch
from .listener import Listener, TapeTransport
from .types import RawMessage

TAPES_DIR = Path(__file__).resolve().parent.parent / "tapes"


@dataclass
class ReplayReport:
    events: int = 0
    buys: int = 0
    sells: int = 0
    skipped: int = 0
    rejected: int = 0
    realized_usd: float = 0.0
    contracts_bought: int = 0
    budget: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)
    decisions: list[str] = field(default_factory=list)
    finished: bool = True

    def to_text(self) -> str:
        lines = [
            f"events     {self.events}",
            f"buys       {self.buys}  ({self.contracts_bought} contracts)",
            f"sells      {self.sells}",
            f"skipped    {self.skipped}   rejected {self.rejected}",
            f"realised   ${self.realized_usd:,.2f}",
            f"latency    worst {self.budget.get('worst_ms', 0)}ms  "
            f"p95 {self.budget.get('p95_ms', 0)}ms  "
            f"budget {self.budget.get('budget_ms', 0)}ms  "
            f"breaches {self.budget.get('breaches', 0)}",
        ]
        if self.reasons:
            lines.append("skip/reject reasons:")
            for reason, count in sorted(self.reasons.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {count:>3}  {reason}")
        if self.decisions:
            lines.append("decisions:")
            lines.extend(f"  {d}" for d in self.decisions)
        return "\n".join(lines)


def load_tape(path: str | Path) -> list[RawMessage]:
    """Tape file: {"messages": [...], "quotes": {...}} or a bare message list."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data["messages"] if isinstance(data, dict) else data
    tape: list[RawMessage] = []
    for item in raw:
        tape.append(RawMessage(
            channel=item.get("channel", ""),
            channel_id=item.get("channel_id", ""),
            text=item.get("text", ""),
            message_id=item.get("message_id", ""),
            ts=float(item.get("ts", 0.0)),
        ))
    return tape


def load_quotes(path: str | Path) -> dict[str, list[list[float]]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    quotes = data.get("quotes", {}) if isinstance(data, dict) else {}
    return {k: [list(map(float, pair)) for pair in v] for k, v in quotes.items()}


def replay(tape: list[RawMessage], quotes: dict[str, list[list[float]]] | None = None,
           *, budget_ms: int = 10_000, broker: Any = None,
           clock: Callable[[], float] | None = None,
           heartbeat_path: Path | None = None,
           kill_path: Path | None = None,
           engine_config: EngineConfig | None = None) -> ReplayReport:
    """Run the pipeline offline. Deterministic for a given tape."""
    import time
    clock = clock or time.time
    engine = Engine(config=engine_config or EngineConfig(), fills=FillLog(), clock=clock)
    broker = broker or PaperBroker()
    heartbeat = Heartbeat(
        path=heartbeat_path or Path(tempfile.gettempdir()) / "mff-replay-heartbeat.json",
        clock=clock)
    kill = KillSwitch(path=kill_path or Path(tempfile.gettempdir()) / "mff-replay-NO_TRADE",
                      clock=clock)
    listener = Listener(
        transport=TapeTransport(tape),
        engine=engine,
        broker=broker,
        quotes=QuoteScript({k: [(float(p[0]), float(p[1])) for p in v]
                            for k, v in (quotes or {}).items()}),
        budget=LatencyBudget(budget_ms=budget_ms),
        heartbeat=heartbeat,
        kill=kill,
        clock=clock,
    )
    asyncio.run(listener.run())

    fills = engine.fills
    report = ReplayReport(
        events=listener.processed,
        buys=len(fills.bought()),
        sells=len(fills.sold()),
        skipped=len([f for f in fills.skipped() if f.event == "skip"]),
        rejected=len([f for f in fills.skipped() if f.event == "reject"]),
        realized_usd=round(engine.stats.realized_usd, 2),
        contracts_bought=sum(f.qty for f in fills.bought()),
        budget=listener.budget.summary(),
        reasons=fills.reasons(),
    )
    for fill in fills.records:
        if fill.event in {"buy", "sell"}:
            report.decisions.append(
                f"{fill.event:<4} {fill.qty:>3}x {fill.contract_key:<28} "
                f"@ {fill.price:<7.2f} {fill.reason}")
        elif fill.event in {"skip", "reject"}:
            report.decisions.append(f"{fill.event:<6} {'':<26} {fill.reason}")
    return report


def replay_file(path: str | Path, **kwargs: Any) -> ReplayReport:
    return replay(load_tape(path), load_quotes(path), **kwargs)

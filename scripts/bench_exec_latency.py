#!/usr/bin/env python3
"""Post-to-order-intent latency, before (legacy runtime path) vs after (exec-core session).

Real timings on this machine, real SQLite/fsync on local disk, no network.

BEFORE = what scorpion.runtime does today for an Options King post, followed by the existing
FastPathPreparer: durable receipt (FULL-sync SQLite txn, to_thread) -> Pipeline._process_serialized
(SQLite normalized commit, to_thread) -> FastPathPreparer.prepare + dispatch to PaperBroker.

AFTER = PaperExecutionSession: on_post (parse + frozen rules + schedule, in memory) and
on_entry_due (latch mirror + O(1) monotonic quote read + Decimal limit + PaperBroker). The
journal is fsync'd by its own thread; its durable-ack latency is reported separately.

  PYTHONPATH=src python scripts/bench_exec_latency.py --samples 2000 --out var/latency.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from scorpion.alerts import Pager
from scorpion.broker import ExecutionMode
from scorpion.config import CHANNELS, EXECUTION_COMPONENT, GUILD_ID, load_forward_rules
from scorpion.decision_packet import DecisionDisposition, OperatorDecisionPacket
from scorpion.domain import EffectKind, EventKind, RawDiscordMessage
from scorpion.durable_ingress import append_raw_with_receipt
from scorpion.fail_safe_control import NoTradeSafetyLatch
from scorpion.fastpath import FastPathPreparer, FastPathStatus, QuoteCache
from scorpion.pipeline import Pipeline
from scorpion.resilience import OperationalMode
from scorpion.session_service import Journal, LatchMirror, PaperExecutionSession, percentile
from scorpion.store import Store

TICKERS = ("ABCD", "EFGH", "IJKL", "MNOP", "QRST", "UVWX")


def letters(i: int) -> str:
    out = ""
    for _ in range(5):
        out = chr(65 + i % 26) + out
        i //= 26
    return out


def post(i: int, when: datetime, ticker: str) -> RawDiscordMessage:
    ts = when + timedelta(milliseconds=i)
    text = (
        f"CALL / TICKER {ticker} / STRIKE ${10 + i % 7}.5 / Exp Nov 20 / "
        f"Current BID Price $.{40 + i % 50} / SL at 25% / @everyone"
    )
    return RawDiscordMessage(f"bench-{i}", GUILD_ID, CHANNELS["options-king"], "author", text, ts, ts)


def stats(values_ns: list[int]) -> dict[str, float]:
    ordered = sorted(values_ns)
    return {
        "n": len(ordered),
        "p50_ms": round(percentile(ordered, 50) / 1e6, 4),
        "p99_ms": round(percentile(ordered, 99) / 1e6, 4),
        "max_ms": round(ordered[-1] / 1e6, 4),
    }


async def before(samples: int, root: Path, when: datetime) -> dict[str, object]:
    cache = QuoteCache()
    preparer = FastPathPreparer(cache, mode=ExecutionMode.PAPER, quote_max_age=timedelta(hours=1))
    totals: list[int] = []
    intents = 0
    for i in range(samples):
        # Fresh store per sample (setup untimed): the legacy reducer caps open positions at 2,
        # so reusing one store would stop producing order intents and flatter the baseline.
        store = Store(root / f"before-{i}.db")
        pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
        raw = post(i, when, letters(i))  # unique contract per post: no position reuse
        started = time.perf_counter_ns()
        await asyncio.to_thread(append_raw_with_receipt, store.path, raw)
        event, effects = await asyncio.to_thread(
            pipeline._process_serialized, raw, persist_raw=False, register_receipt=False
        )
        for effect in effects:
            if effect.kind is EffectKind.PROPOSE_OPEN and effect.contract_key:
                cache.update(effect.contract_key, bid=Decimal("0.40"), ask=Decimal("0.45"),
                             observed_ts_utc=datetime.now(UTC))
                packet = OperatorDecisionPacket(
                    "p", "v3", event.event_id, raw.message_id, event.kind, effect.contract_key,
                    DecisionDisposition.READY_FOR_OPERATOR_REVIEW, 1.0, "r", 1.0, "a", 1.0,
                    OperationalMode.NORMAL, (), (), 0.0, (), datetime.now(UTC),
                )
                intent = preparer.prepare(event, effect, packet, quantity=1)
                if intent.status in (FastPathStatus.PAPER_READY, FastPathStatus.WAITING_FOR_LIMIT):
                    if intent.status is FastPathStatus.PAPER_READY:
                        preparer.dispatch(intent)
                    intents += 1
        totals.append(time.perf_counter_ns() - started)
        if event.kind is not EventKind.ENTRY:
            raise SystemExit(f"benchmark post did not parse as ENTRY: {event.reason}")
    return {"post_to_intent": stats(totals), "intents": intents,
            "note": "fresh SQLite store per sample (WAL, FULL sync), setup excluded from timing"}


def after(samples: int, root: Path, when: datetime) -> dict[str, object]:
    rules = load_forward_rules()
    latch_db = NoTradeSafetyLatch(root / "latch.db")
    latch_db.trip_no_trade(EXECUTION_COMPONENT, reason="bench-init")
    latch_db.clear_no_trade(EXECUTION_COMPONENT, operator="bench", reason="bench arm")
    durable: dict[int, int] = {}
    journal = Journal(root / "journal.jsonl", on_durable=lambda seq, ns: durable.__setitem__(seq, ns))
    cache = QuoteCache()
    session = PaperExecutionSession(
        rules=rules, cache=cache, latch=LatchMirror(latch_db), pager=Pager(routing_key=""),
        journal=journal, allowed_author_ids=frozenset({"author"}),
    )
    totals: list[int] = []
    submitted: dict[int, int] = {}
    for i in range(samples):
        ticker = TICKERS[i % len(TICKERS)]
        cache.update(ticker, bid=Decimal("7.40"), ask=Decimal("7.42"), last=Decimal("7.41"),
                     observed_ts_utc=datetime.now(UTC))
        raw = post(i, when, ticker)
        started = time.perf_counter_ns()
        decision = session.on_post(raw, recv_ns=started)
        result = session.on_entry_due(decision.call_id) if decision.call_id else None
        totals.append(time.perf_counter_ns() - started)
        if result is None:
            raise SystemExit(f"after path produced no intent: {decision.action}")
        submitted[journal._seq] = started
        session.working.pop(decision.call_id, None)
    # Isolated ack: one post at a time (posts arrive minutes apart in practice), waiting for the
    # fsync of each before the next, so the number is the disk, not a synthetic burst backlog.
    isolated: list[int] = []
    for i in range(min(200, samples)):
        ticker = TICKERS[i % len(TICKERS)]
        raw = post(samples + i, when, ticker)
        started = time.perf_counter_ns()
        decision = session.on_post(raw, recv_ns=started)
        session.on_entry_due(decision.call_id)
        seq = journal._seq
        while seq not in durable:
            time.sleep(0.00005)
        isolated.append(durable[seq] - started)
        session.working.pop(decision.call_id, None)
    journal.close()
    acks = [durable[seq] - t0 for seq, t0 in submitted.items() if seq in durable]
    return {
        "post_to_intent": stats(totals),
        "post_to_decision": stats(session.latency.samples_ns["post_to_decision"]),
        "entry_to_intent": stats(session.latency.samples_ns["entry_to_intent"]),
        "journal_durable_ack_burst": stats(acks),
        "journal_durable_ack_isolated": stats(isolated),
        "intents": len(session.orders),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=2000)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    when = datetime(2026, 10, 29, 13, 40, tzinfo=UTC)  # Thu 09:40 ET, main track
    with tempfile.TemporaryDirectory(prefix="exec-bench-") as tmp:
        root = Path(tmp)
        result = {
            "machine": platform.platform(), "python": sys.version.split()[0],
            "samples": a.samples,
            "before_legacy_runtime_path": asyncio.run(before(a.samples, root, when)),
            "after_exec_core_session": after(a.samples, root, when),
        }
    text = json.dumps(result, indent=2)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .broker import Quote
from .config import Policy
from .domain import EventKind, SignalEvent
from .outbox import Dispatcher
from .paper import PaperSender
from .pipeline import Pipeline
from .reconciliation import BrokerPosition, reconcile
from .replay import replay
from .scenario import demo_messages, demo_quotes, run
from .store import Store
from .tca import report_from_store


def health_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scorpion.db")
    args = parser.parse_args()
    print(json.dumps(Store(args.db).health_snapshot(), indent=2, sort_keys=True))


def replay_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("events", type=Path, help="JSONL of normalized SignalEvent-like records")
    args = parser.parse_args()
    events: list[SignalEvent] = []
    for line in args.events.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        row["kind"] = EventKind(row["kind"])
        row["source_ts_utc"] = datetime.fromisoformat(row["source_ts_utc"]).astimezone(UTC)
        row["received_ts_utc"] = datetime.fromisoformat(row["received_ts_utc"]).astimezone(UTC)
        events.append(SignalEvent(**row))
    state, effects = replay(events)
    print(json.dumps({
        "halted": state.halted,
        "positions": {k: v.status.value for k, v in state.positions.items()},
        "effects": [e.kind.value for e in effects],
    }, indent=2, sort_keys=True))


# ── operator surface ─────────────────────────────────────────────────────────
# The pieces existed; nothing tied them together. `demo` runs the whole stack against a
# paper venue (no credentials, no network) and prints the trail, which is both the fastest
# way to see the system work and the harness a shadow run will use.


def _load_quotes(path: Path, now: datetime) -> dict[str, Quote]:
    """`{"KEY": {"bid": "1.00", "ask": "1.02", "age_ms": 120}}`."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Quote] = {}
    for key, row in payload.items():
        out[key] = Quote(
            bid=Decimal(str(row["bid"])),
            ask=Decimal(str(row["ask"])),
            observed_ts_utc=now - timedelta(milliseconds=int(row.get("age_ms", 0))),
        )
    return out


def cmd_demo(args: argparse.Namespace) -> int:
    directory = Path(tempfile.mkdtemp(prefix="scorpion-demo-"))
    db_path = directory / "demo.db"
    start = datetime.now(UTC).replace(microsecond=0)
    result = run(
        store=Store(db_path),
        messages=demo_messages(start),
        quotes=demo_quotes(),
        policy=Policy(live_submission_enabled=True),
        sender=PaperSender(),
    )
    print("scenario trail")
    for step in result.steps:
        print(f"  {step}")
    print()
    print(f"  sent {result.sent} | refused {result.refused} | open {result.open_positions} "
          f"| realized {result.realized_usd}")
    print(f"  tca {json.dumps(result.tca.to_dict(), sort_keys=True)}")
    print(f"  verified (book == applied log): {result.verified}")
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    print(f"  db {db_path}")
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    store = Store(args.db)
    moment = datetime.now(UTC)
    quotes = _load_quotes(Path(args.quotes), moment) if args.quotes else {}
    dispatcher = Dispatcher(
        store=store, sender=PaperSender(),
        policy=Policy(live_submission_enabled=args.live), owner=args.owner,
    )
    state, _ = replay(store.load_signals())
    report = dispatcher.dispatch_once(state=state, now=moment, quotes=quotes)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.dead == 0 else 1


def cmd_outbox(args: argparse.Namespace) -> int:
    print(json.dumps(Store(args.db).outbox_snapshot(), indent=2, sort_keys=True))
    return 0


def cmd_tca(args: argparse.Namespace) -> int:
    report = report_from_store(Store(args.db).load_executions())
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.n else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    store = Store(args.db)
    state, _ = replay(store.load_signals())
    rows = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    positions = [BrokerPosition(str(r["contract_key"]), int(r["quantity"])) for r in rows]
    result = reconcile(state, positions)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if not result.clean:
        store.set_halt(True, result.reason)
        print(f"halted: {result.reason}", file=sys.stderr)
        return 2
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    """Read the corpus, summarise the edge, and state what it can be wrong about."""
    from .ledger import build_report, load_csv
    from .ruin import RuinConfig
    from .ruin import report as ruin_report

    ledger = load_csv(args.corpus)
    summary = build_report(ledger)
    rs = [float(r) for r in ledger.rs()]
    sizing = ruin_report(rs, RuinConfig(
        runs=args.runs, trades_per_run=args.trades, risk_fraction=args.risk,
    ))

    payload = {
        "manifest": summary["manifest"],
        "r": summary["r"],
        "per_channel": summary["per_channel"],
        "edge_breakeven": sizing["edge_breakeven"],
        "win_haircut": sizing["win_haircut"],
        "stress": sizing["stress"],
        "simulation": sizing["simulation"],
        "recommended_fraction": sizing["recommended_fraction"],
        "diagnostics": sizing["diagnostics"],
        "caveats": sizing["caveats"],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    m, r = payload["manifest"], payload["r"]
    print(f"corpus {m['trades']} trades / {m['trading_days']} days / "
          f"{m['first_day']}..{m['last_day']}  digest {m['digest'][:12]}")
    print(f"  channels   {', '.join(m['channels'])}")
    print(f"  outcomes   {m['outcomes']}")
    print(f"  rejected   {m['rejected']}  near-duplicates {m['near_duplicates']}")
    print(f"  R          mean {r['mean_r']}  median {r['median_r']}  total {r['total_r']}"
          f"  win% {r['win_rate']}  PF {r['profit_factor']}")
    edge = payload["edge_breakeven"]
    print(f"  breakeven  wins can be overstated by "
          f"{100 * edge['breakeven_win_haircut']:.1f}% at a {100 * edge['loss_rate']:.0f}% "
          f"loss rate (avg win {edge['avg_win_r']}R, avg loss {edge['avg_loss_r']}R)")
    print("  win haircut -> mean R / P(down) / median growth")
    for row in payload["win_haircut"]:
        print(f"    {100 * row['win_haircut']:>4.0f}%  {row['mean_r']:+.3f}R   "
              f"{row['p_down']:>5.1f}%   {row['median_growth']:.2f}x")
    print(f"  size       {100 * payload['recommended_fraction']:.2f}% of equity per trade")
    for line in payload["diagnostics"]:
        print(f"  ! {line}")
    return 0


def cmd_markout(args: argparse.Namespace) -> int:
    """Entry drift vs exit capture, with the coverage refusals stated up front."""
    from .ledger import load_csv
    from .markout import load_bars, run

    ledger = load_csv(args.corpus)
    bars = load_bars(args.bars)
    payload = run(ledger, bars)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    agg, clock = payload["aggregate"], payload["clock"]
    cov = agg["coverage"]
    print(f"bar sets {payload['contracts_in_bars']}  measurable {cov['measurable']}"
          f"/{cov['total']}  excluded {cov['excluded'] or '{}'}")
    if "withheld" in agg:
        print(f"  withheld: {agg['withheld']}")
    else:
        print(f"  capture   median {agg['median_capture']}  mean {agg['mean_capture']}"
              f"  >=50% kept {agg['captured_over_half']}%")
        print(f"  drift     median 15m {agg['median_entry_drift_15m']}   "
              f"available {agg['median_available']}")
        print(f"  verdict   {agg['verdict']}")
    print(f"  clock     first {clock['first_entry_utc']}  last {clock['last_entry_utc']}")
    print("    by hour (UTC): " + ", ".join(f"{k} {v}" for k, v in clock["by_hour_utc"].items()))
    return 0


def cmd_faults(args: argparse.Namespace) -> int:
    """Inject every known fault into a fresh store and report containment."""
    import tempfile
    from pathlib import Path as _Path

    from .faults import render, run, to_json

    root = _Path(tempfile.mkdtemp(prefix="scorpion-faults-"))
    report = run(lambda name: Store(root / f"{name}.db"))
    if args.json:
        print(to_json(report))
    else:
        print(render(report))
    return 0 if report.ok else 1


def cmd_supervision(args: argparse.Namespace) -> int:
    """Liveness, the restart decision, and the metrics that explain a refusal."""
    from datetime import timedelta as _td

    from .ops import render, report, to_json

    store = Store(args.db)
    payload = report(store, stale_after=_td(seconds=args.stale_after))
    if args.json:
        print(to_json(payload))
    else:
        print(render(payload))
    return int(payload["assessment"]["exit_code"])


def cmd_halt(args: argparse.Namespace) -> int:
    """Stop the desk. Recorded as an applied event, so a replay stays halted."""
    from .snapshot import state_from_snapshot

    store = Store(args.db)
    state, _ = state_from_snapshot(store)
    pipeline = Pipeline(store=store)
    pipeline.state = state
    changed = pipeline.halt(args.reason)
    print(json.dumps({"halted": True, "changed": changed, "reason": args.reason}, indent=2))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from .snapshot import state_from_snapshot

    store = Store(args.db)
    state, _ = state_from_snapshot(store)
    pipeline = Pipeline(store=store)
    pipeline.state = state
    changed = pipeline.clear_halt()
    print(json.dumps({"halted": False, "changed": changed}, indent=2))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Export one day's decisions as a sealed, checkable bundle."""
    from .audit import build
    from .snapshot import state_from_snapshot

    store = Store(args.db)
    state, _ = state_from_snapshot(store)
    bundle = build(store, state, date.fromisoformat(args.day))
    if args.out:
        path = bundle.write(args.out)
        print(json.dumps({**bundle.to_dict(), "path": str(path)}, indent=2))
    else:
        print(bundle.payload_json)
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    """What the corpus can support, and the missing input behind every refusal."""
    from .bars import load_path, persist
    from .coverage import assess, to_dict
    from .ledger import load_csv

    rows_bars, rejected, digest = load_path(args.bars)
    ledger = load_csv(args.corpus)
    classified, summary = assess(ledger, {b.contract_key: b for b in rows_bars})
    payload = to_dict(classified, summary)
    payload["bars"] = {"accepted": len(rows_bars), "rejected": rejected, "digest": digest[:16]}
    if args.db:
        store = Store(args.db)
        payload["persisted"] = persist(store, rows_bars)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    m = payload["summary"]
    print(f"corpus {m['trades']} trades  measurable {m['measurable']} ({m['pct']}%)  "
          f"bar sets {m['contracts_with_bars']}")
    print("  by reason  " + (", ".join(f"{k} {v}" for k, v in m["by_reason"].items()) or "none"))
    for channel, entry in m["by_channel"].items():
        print(f"    {channel:26s} {entry['measurable']:3d}/{entry['total']:<3d} "
              f"{entry['pct']:5.1f}%")
    if rejected:
        print(f"  bar rejections: {len(rejected)}")
        for row in rejected[:5]:
            print(f"    {row['set']}: {row['code']} {row['detail'][:60]}")
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    from .sessions import report as calendar_report

    payload = calendar_report(date.fromisoformat(args.start), date.fromisoformat(args.end))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Rebuild the book from its applied log and diff it against the live state.

    Exit 2 on divergence, and the store is halted: a book that cannot be reproduced from
    its own log must not keep trading.
    """
    from .snapshot import SnapshotError, state_from_snapshot
    from .verify import verify_log

    store = Store(args.db)
    result = verify_log(store)
    if not result.ok:
        store.set_halt(True, "replay_divergence:" + ",".join(
            d.describe() for d in result.divergences[:3]
        ))
    # Resume from the snapshot, not from the surviving tail: after compaction a tail-only
    # replay starts from an empty book and every position in it looks orphaned.
    try:
        state, from_snapshot = state_from_snapshot(store)
    except SnapshotError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    payload = result.to_dict()
    payload["replayed"] = {
        "positions": {k: {"status": v.status.value, "quantity": v.quantity}
                      for k, v in state.positions.items()},
        "equity": str(state.risk.equity),
        "halted": state.halted,
        "resumed_from_seq": from_snapshot,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.ok else 2


def cmd_recover(args: argparse.Namespace) -> int:
    """Quarantine sends that were in flight when a process died."""
    store = Store(args.db)
    recovered = store.recover_sending(
        now=datetime.now(UTC), older_than_seconds=args.older_than
    )
    print(json.dumps({"recovered": len(recovered), "unknown": store.load_unknown()},
                     indent=2, sort_keys=True))
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    """Snapshot the book, truncate the covered log, then prove the result."""
    from .snapshot import SnapshotError, compact, verify_snapshot

    store = Store(args.db)
    try:
        snap = compact(store, keep_tail=args.keep_tail)
    except SnapshotError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    ok, problems = verify_snapshot(store)
    print(json.dumps({
        "ok": ok, "snapshot_seq": snap.seq, "digest": snap.digest[:16],
        "rows_remaining": len(store.load_applied()), "problems": problems,
    }, indent=2, sort_keys=True))
    return 0 if ok else 2


def cmd_unknown(args: argparse.Namespace) -> int:
    """List sends whose fate is unknown. Exit 1 when any exist, so cron can page."""
    store = Store(args.db)
    rows = store.load_unknown()
    print(json.dumps({
        "unknown": len(rows),
        "effects": [
            {"effect_id": r["effect_id"], "contract_key": r["contract_key"],
             "reason": r["last_error"], "idempotency_key": r.get("idempotency_key", "")}
            for r in rows
        ],
    }, indent=2, sort_keys=True))
    return 0 if not rows else 1


def cmd_health(args: argparse.Namespace) -> int:
    print(json.dumps(Store(args.db).health_snapshot(), indent=2, sort_keys=True))
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    """Run the dispatcher as a service: one bounded pass per tick, until told to stop."""
    from .service import Service, ServiceConfig
    from .snapshot import SnapshotError

    store = Store(args.db)
    moment = datetime.now(UTC)
    quotes = _load_quotes(Path(args.quotes), moment) if args.quotes else {}
    references: dict[str, Decimal] = {}
    if args.references:
        references = {
            key: Decimal(str(value))
            for key, value in json.loads(
                Path(args.references).read_text(encoding="utf-8")
            ).items()
        }
    config = ServiceConfig(
        cadence_seconds=args.cadence,
        jitter_seconds=args.jitter,
        idle_backoff_seconds=args.idle_backoff,
        max_backoff_seconds=args.max_backoff,
        max_ticks=args.max_ticks,
        owner=args.owner,
        live=args.live,
        series_path=Path(args.series) if args.series else None,
        series_cap=args.series_cap,
        reconcile_on_unknown=not args.no_reconcile,
        recover_after_seconds=args.recover_after,
        stale_after_seconds=args.stale_after,
    )
    service = Service(
        store,
        sender=PaperSender(),
        policy=Policy(live_submission_enabled=args.live),
        quotes=quotes,
        references=references,
        config=config,
        seed=args.seed,
    )
    try:
        if args.once:
            report = service.step()
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
            return report.exit_code
        return service.run()
    except SnapshotError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2


def cmd_series(args: argparse.Namespace) -> int:
    """Summarise the metrics series a service writes."""
    from .service import read_series, render_series, summarize_series

    summary = summarize_series(read_series(Path(args.path), tail=args.tail))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(render_series(summary))
    return 0


def cmd_signature(args: argparse.Namespace) -> int:
    """Compute a truncated path signature, with the fit gate stated up front."""
    from .signature import SignatureError, lead_lag, render, report

    if args.path:
        raw = json.loads(Path(args.path).read_text(encoding="utf-8"))
    else:
        raw = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
    if not isinstance(raw, list):
        print(json.dumps({"ok": False, "error": "path must be a JSON array"}), file=sys.stderr)
        return 2
    try:
        if raw and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in raw
        ):
            payload = report(
                lead_lag([float(value) for value in raw]), level=args.level, samples=args.samples
            )
            payload["lead_lag"] = True
        else:
            payload = report(raw, level=args.level, samples=args.samples)
    except SignatureError as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render(payload))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scorpion", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    demo = sub.add_parser("demo", help="run the full stack against a paper venue")
    demo.add_argument("--json", action="store_true")
    demo.set_defaults(func=cmd_demo)

    dispatch = sub.add_parser("dispatch", help="one outbox pass over the store")
    dispatch.add_argument("--db", default="scorpion.db")
    dispatch.add_argument("--quotes", default=None, help="JSON map contract_key -> bid/ask")
    dispatch.add_argument("--live", action="store_true", help="allow real submission")
    dispatch.add_argument("--owner", default="cli")
    dispatch.set_defaults(func=cmd_dispatch)

    outbox = sub.add_parser("outbox", help="delivery state counts")
    outbox.add_argument("--db", default="scorpion.db")
    outbox.set_defaults(func=cmd_outbox)

    tca = sub.add_parser("tca", help="execution cost report")
    tca.add_argument("--db", default="scorpion.db")
    tca.set_defaults(func=cmd_tca)

    rec = sub.add_parser("reconcile", help="compare the book with a venue snapshot")
    rec.add_argument("--db", default="scorpion.db")
    rec.add_argument("--snapshot", required=True)
    rec.set_defaults(func=cmd_reconcile)

    res = sub.add_parser("research", help="corpus edge, win-haircut breakeven, sizing")
    res.add_argument("--corpus", default="mff-backtest/trades.csv")
    res.add_argument("--runs", type=int, default=400)
    res.add_argument("--trades", type=int, default=250)
    res.add_argument("--risk", type=float, default=0.015)
    res.add_argument("--json", action="store_true")
    res.set_defaults(func=cmd_research)

    mk = sub.add_parser("markout", help="entry drift vs exit capture, with coverage refusals")
    mk.add_argument("--corpus", default="mff-backtest/trades.csv")
    mk.add_argument("--bars", default="mff-backtest/week_bars.json")
    mk.add_argument("--json", action="store_true")
    mk.set_defaults(func=cmd_markout)

    flt = sub.add_parser("faults", help="inject every known fault and report containment")
    flt.add_argument("--json", action="store_true")
    flt.set_defaults(func=cmd_faults)

    sup = sub.add_parser("supervision", help="liveness, restart decision and metrics")
    sup.add_argument("--db", default="scorpion.db")
    sup.add_argument("--stale-after", type=int, default=1800)
    sup.add_argument("--json", action="store_true")
    sup.set_defaults(func=cmd_supervision)

    h = sub.add_parser("halt", help="stop the desk; recorded in the applied log")
    h.add_argument("--db", default="scorpion.db")
    h.add_argument("--reason", default="operator")
    h.set_defaults(func=cmd_halt)

    rs = sub.add_parser("resume", help="re-arm a halted desk; recorded in the applied log")
    rs.add_argument("--db", default="scorpion.db")
    rs.set_defaults(func=cmd_resume)

    au = sub.add_parser("audit", help="export a day's decisions as a sealed bundle")
    au.add_argument("--db", default="scorpion.db")
    au.add_argument("--day", required=True)
    au.add_argument("--out", default="")
    au.set_defaults(func=cmd_audit)

    cov = sub.add_parser("coverage", help="which trades can be measured, and what is missing")
    cov.add_argument("--corpus", default="mff-backtest/trades.csv")
    cov.add_argument("--bars", default="mff-backtest/week_bars.json")
    cov.add_argument("--db", default="")
    cov.add_argument("--json", action="store_true")
    cov.set_defaults(func=cmd_coverage)

    cal = sub.add_parser("calendar", help="sessions, half-days and holiday coverage")
    cal.add_argument("--start", default="2026-01-01")
    cal.add_argument("--end", default="2026-12-31")
    cal.set_defaults(func=cmd_calendar)

    ver = sub.add_parser("verify", help="replay the applied log and diff against the book")
    ver.add_argument("--db", default="scorpion.db")
    ver.set_defaults(func=cmd_verify)

    rec = sub.add_parser("recover", help="quarantine sends that were in flight at a crash")
    rec.add_argument("--db", default="scorpion.db")
    rec.add_argument("--older-than", type=int, default=0)
    rec.set_defaults(func=cmd_recover)

    comp = sub.add_parser("compact", help="snapshot the book and truncate the covered log")
    comp.add_argument("--db", default="scorpion.db")
    comp.add_argument("--keep-tail", type=int, default=0)
    comp.set_defaults(func=cmd_compact)

    unk = sub.add_parser("unknown", help="list sends whose fate is unknown (exit 1 if any)")
    unk.add_argument("--db", default="scorpion.db")
    unk.set_defaults(func=cmd_unknown)

    health = sub.add_parser("health", help="heartbeats, halt flag, pending effects")
    health.add_argument("--db", default="scorpion.db")
    health.set_defaults(func=cmd_health)

    svc = sub.add_parser("service", help="run the dispatcher loop with jittered cadence")
    svc.add_argument("--db", default="scorpion.db")
    svc.add_argument("--quotes", default=None, help="JSON map contract_key -> bid/ask")
    svc.add_argument("--references", default=None, help="JSON map contract_key -> his bid")
    svc.add_argument("--live", action="store_true", help="allow real submission")
    svc.add_argument("--owner", default="service")
    svc.add_argument("--cadence", type=float, default=1.0)
    svc.add_argument("--jitter", type=float, default=0.25)
    svc.add_argument("--idle-backoff", type=float, default=2.0)
    svc.add_argument("--max-backoff", type=float, default=60.0)
    svc.add_argument("--max-ticks", type=int, default=None)
    svc.add_argument("--once", action="store_true", help="run one tick and exit")
    svc.add_argument("--series", default=None, help="path to the metrics series JSONL")
    svc.add_argument("--series-cap", type=int, default=10000)
    svc.add_argument("--no-reconcile", action="store_true")
    svc.add_argument("--recover-after", type=int, default=0)
    svc.add_argument("--stale-after", type=int, default=1800)
    svc.add_argument("--seed", type=int, default=None)
    svc.set_defaults(func=cmd_service)

    srs = sub.add_parser("series", help="summarise a service metrics series")
    srs.add_argument("--path", required=True)
    srs.add_argument("--tail", type=int, default=None)
    srs.add_argument("--json", action="store_true")
    srs.set_defaults(func=cmd_series)

    sig = sub.add_parser("signature", help="compute a truncated path signature")
    sig.add_argument("--path", default=None,
                     help="JSON array of vertices, or of numbers for a lead-lag series")
    sig.add_argument("--level", type=int, default=3)
    sig.add_argument("--samples", type=int, default=107,
                     help="corpus size against which the fit gate is measured")
    sig.add_argument("--json", action="store_true")
    sig.set_defaults(func=cmd_signature)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

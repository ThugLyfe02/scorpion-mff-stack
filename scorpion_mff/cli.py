"""Operator CLI: doctor / replay / arm / status.

    python -m scorpion_mff.cli doctor
    python -m scorpion_mff.cli replay tapes/sep8.json
    python -m scorpion_mff.cli arm            # dry-armed (no orders leave the box)
    python -m scorpion_mff.cli arm --live     # needs MFF_RH_TOKEN, explicit opt-in
    python -m scorpion_mff.cli status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .broker import HttpQuoteProvider, PaperBroker, RobinhoodAgenticBroker, mask
from .context import (ContextError, attribute as attribute_context,
                      attribution_text, load as load_context)
from .engine import Engine, EngineConfig
from .evidence import (SOURCE_CORPUS, build_report, load_corpus, load_fills,
                       to_markdown, to_text)
from .fills import FillLog
from .health import Heartbeat, LatencyBudget
from .killswitch import KillSwitch
from .listener import Listener, WebsocketGateway
from .parser import CHANNEL_IDS, EXCLUDED_CHANNEL_IDS, EXCLUDED_TICKERS
from .replay import load_quotes, load_tape, replay
from .risk import RiskBudget, RiskBudgetError

ROOT = Path(__file__).resolve().parent.parent
FILL_LOG = ROOT / "live" / "fills.jsonl"
HEARTBEAT = ROOT / "live" / "heartbeat.json"
NO_TRADE = ROOT / "live" / "NO_TRADE"
RISK_BUDGET = ROOT / "live" / "risk-budget.json"

__version__ = "0.2.0"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scorpion-mff", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"scorpion-mff {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="show channel/rule/switch/broker state; touches nothing")

    rep = sub.add_parser("replay", help="run a recorded tape through the live pipeline")
    rep.add_argument("tape", type=Path)
    rep.add_argument("--json", action="store_true", help="machine-readable report")
    rep.add_argument("--no-pipe-test", action="store_true",
                     help="size the first trade at full clip (the pre-rule counterfactual)")

    arm = sub.add_parser("arm", help="run the live Gateway listener")
    arm.add_argument("--live", action="store_true",
                     help="place REAL orders (needs MFF_RH_TOKEN)")
    arm.add_argument("--limit", type=int, default=None, help="stop after N events")

    sub.add_parser("status", help="kill-switch state + tail of the fill log")

    bt = sub.add_parser("backtest", help="run the executor over the historical alert corpus")
    bt.add_argument("--corpus", type=Path, default=None, help="trades.json (default: mff-backtest/bt_full)")
    bt.add_argument("--ask-multiple", type=float, default=1.05,
                    help="how far through his posted bid we pay (default 1.05)")
    bt.add_argument("--exit-slippage", type=float, default=1.0,
                    help="fraction of his posted exit level we get (default 1.0)")
    bt.add_argument("--no-pipe-test", action="store_true", help="full clip from the first trade")
    bt.add_argument("--bare-exits", action="store_true",
                    help="emit exits that name no contract (measures the ambiguity cost)")
    bt.add_argument("--attribution", action="store_true",
                    help="also show what each locked rule is worth, one rule at a time")
    bt.add_argument("--md", type=Path, default=None, help="write the markdown report here")
    bt.add_argument("--json", action="store_true", help="machine-readable summary")

    wa = sub.add_parser("watch", help="check the hone's heartbeat and page a human if it is ill")
    wa.add_argument("--once", action="store_true",
                    help="single check (for Task Scheduler / cron); this is the only mode")
    wa.add_argument("--stale-after", type=float, default=None,
                    help="heartbeat age that counts as stale, seconds (default 120)")
    wa.add_argument("--dry-run", action="store_true",
                    help="build the page and print it; send nothing")
    wa.add_argument("--heartbeat", type=Path, default=None)
    wa.add_argument("--fills", type=Path, default=None)
    wa.add_argument("--kill", type=Path, default=None)
    wa.add_argument("--json", action="store_true", help="machine-readable state")

    pol = sub.add_parser("policy", help="risk policy: show, validate, init, simulate")
    pol_sub = pol.add_subparsers(dest="policy_cmd", required=True)
    for name, helptext in (("show", "print the policy an operator must read before arming"),
                           ("validate", "parse and check; non-zero exit if it is unsafe")):
        sp = pol_sub.add_parser(name, help=helptext)
        sp.add_argument("--path", type=Path, default=None)
        sp.add_argument("--json", action="store_true")
    init = pol_sub.add_parser("init", help="write the baseline policy (refuses to overwrite)")
    init.add_argument("--path", type=Path, default=None)
    init.add_argument("--name", default="baseline-v1")
    sim = pol_sub.add_parser("simulate", help="run the corpus with the policy active")
    sim.add_argument("--path", type=Path, default=None)
    sim.add_argument("--exit-slippage", type=float, default=1.0,
                     help="stress: fraction of his posted exit level we get (1.0 = as recorded)")
    sim.add_argument("--ask-multiple", type=float, default=1.05,
                     help="stress: how far through his bid we pay (default 1.05)")
    sim.add_argument("--json", action="store_true")

    desk = sub.add_parser("desk", help="expectancy, drawdown and allocation from the record")
    desk.add_argument("--risk-unit", type=float, default=None, help="1R in USD (default: policy, else $1500)")
    desk.add_argument("--json", action="store_true")
    desk.add_argument("--md", type=Path, default=None, help="write the markdown report here")

    tca = sub.add_parser("tca", help="execution quality: path analysis and venue fills")
    tca_sub = tca.add_subparsers(dest="tca_cmd", required=True)
    tp = tca_sub.add_parser("path", help="what the contract did between entry and exit (real bars)")
    tp.add_argument("--bars", type=Path, default=None)
    tp.add_argument("--ask-multiple", type=float, default=1.05)
    tp.add_argument("--md", type=Path, default=None)
    tp.add_argument("--json", action="store_true")
    tf = tca_sub.add_parser("fills", help="realised venue fills: limit vs fill, bps, latency")
    tf.add_argument("--executions", type=Path, default=None)
    tf.add_argument("--json", action="store_true")

    fz = sub.add_parser("fuzz", help="throw generated posts at the hot path and check invariants")
    fz.add_argument("--iterations", type=int, default=3000)
    fz.add_argument("--seed", type=int, default=7)
    fz.add_argument("--json", action="store_true")

    rb = sub.add_parser("risk-budget", help="pre-trade worst-case loss and concentration caps")
    rb_sub = rb.add_subparsers(dest="risk_budget_cmd", required=True)
    for name, helptext in (("show", "print the active budget and its defaults"),
                           ("validate", "parse and check; non-zero exit if unsafe")):
        sp = rb_sub.add_parser(name, help=helptext)
        sp.add_argument("--path", type=Path, default=None)
        sp.add_argument("--json", action="store_true")
    rbi = rb_sub.add_parser("init", help="write a conservative budget (refuses to overwrite)")
    rbi.add_argument("--path", type=Path, default=None)
    rbi.add_argument("--total", type=float, default=3000.0,
                     help="max premium at risk across the book (default 2R)")
    rbi.add_argument("--daily", type=float, default=3000.0,
                     help="worst-case daily loss budget (default 2R)")

    evp = sub.add_parser("evidence", help="what the record proves: intervals, posterior, drift")
    ev_sub = evp.add_subparsers(dest="evidence_cmd", required=True)
    evr = ev_sub.add_parser("report", help="measure live fills (or the historical corpus)")
    evr.add_argument("--fills", type=Path, default=None,
                     help="fill-log JSONL (default: live/fills.jsonl)")
    evr.add_argument("--corpus", type=Path, default=None,
                     help="historical trades.json; labels the result CALIBRATION ONLY")
    evr.add_argument("--draws", type=int, default=4000, help="bootstrap draws")
    evr.add_argument("--seed", type=int, default=7)
    evr.add_argument("--context", type=Path, default=None,
                     help="research-context JSON to join (attribution only; never sizes)")
    evr.add_argument("--md", type=Path, default=None, help="write a markdown report here")
    evr.add_argument("--json", action="store_true")
    return p


def cmd_doctor(args: argparse.Namespace) -> int:
    kill = KillSwitch(path=NO_TRADE)
    hb = Heartbeat(path=HEARTBEAT)
    lines = [
        f"scorpion-mff {__version__}",
        "",
        "channels (include):",
        *[f"  {name:<26} {cid}" for name, cid in CHANNEL_IDS.items()],
        "channels (exclude):",
        *[f"  {name:<26} {cid}" for name, cid in EXCLUDED_CHANNEL_IDS.items()],
        f"excluded tickers            {', '.join(sorted(EXCLUDED_TICKERS)) or '(none)'}",
        "",
        f"kill switch                 {kill.status()}   ({kill.path})",
        f"heartbeat                   {hb.path}",
        f"fill log                    {FILL_LOG}",
        "",
        f"discord token               {'present' if os.environ.get('DISCORD_BOT_TOKEN') else 'MISSING (arm will refuse)'}",
        f"robinhood token             {mask(os.environ.get('MFF_RH_TOKEN', ''))}",
        f"robinhood base              {os.environ.get('MFF_RH_BASE', 'https://api.robinhood.com')}",
        f"broker mode                 {'DRY (no orders leave the box)' if not os.environ.get('MFF_RH_LIVE') else 'LIVE'}",
        "",
        "locked rules:",
        "  entry limit = min(live_ask, his_bid * 1.15); skip if ask > +25% off his bid",
        "  first trade of day = 1 contract (pipe test)",
        "  clip: king/etf/platinum $1500 (add $2250) | high-confidence $3000 (add $4500)",
        "  max concurrent 2; add once per position; trim/close -> trim to 1 + BE",
        "  STOP/stand-down halts new buys (exits stay allowed)",
    ]
    print("\n".join(lines))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    tape = load_tape(args.tape)
    quotes = load_quotes(args.tape)
    cfg = EngineConfig(pipe_test=not args.no_pipe_test)
    report = replay(tape, quotes, engine_config=cfg)
    if args.json:
        print(json.dumps({
            "events": report.events, "buys": report.buys, "sells": report.sells,
            "contracts_bought": report.contracts_bought, "realized_usd": report.realized_usd,
            "skipped": report.skipped, "rejected": report.rejected,
            "budget": report.budget, "reasons": report.reasons,
        }, indent=2))
        return 0
    print(f"replay {args.tape}  (pipe_test={cfg.pipe_test})")
    print(report.to_text())
    return 0


def cmd_arm(args: argparse.Namespace) -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("DISCORD_BOT_TOKEN is not set - refusing to arm", file=sys.stderr)
        return 1
    kill = KillSwitch(path=NO_TRADE)
    if kill.active():
        print(f"kill switch is active: {kill.status()}  (delete {kill.path} to arm)", file=sys.stderr)
        return 1

    # The risk policy is a precondition, not a decoration. A missing, invalid or
    # unreviewed policy is a refusal: the desk does not trade without a written
    # risk limit, and it does not trade on one nobody has looked at in a week.
    from . import policy as policy_mod
    from .governor import GovernorState, make_governor

    policy_path = ROOT / "live" / "policy.json"
    try:
        risk_policy = policy_mod.load(policy_path)
    except policy_mod.PolicyError as exc:
        print(f"{exc}\nrefusing to arm: no valid risk policy at {policy_path}", file=sys.stderr)
        return 1
    if risk_policy.is_stale():
        age_h = (risk_policy.age_minutes() or 0) / 60
        print(f"policy {risk_policy.name} is {age_h:.1f}h old (limit "
              f"{risk_policy.stale_after_minutes / 60:.0f}h) - review and re-date it, "
              f"then arm", file=sys.stderr)
        return 1

    state_path = ROOT / "live" / "governor.json"
    try:
        risk_state = GovernorState.load(state_path)
    except ValueError as exc:
        print(f"{exc}\nrefusing to arm: a corrupt risk file could silently reset a "
              f"drawdown limit", file=sys.stderr)
        return 1
    if risk_state.halted_reason:
        print(f"risk state is stood down: {risk_state.halted_reason}\n"
              f"delete {state_path} after a review to re-arm", file=sys.stderr)
        return 1

    # An optional pre-trade budget. A corrupt file is a refusal, not a silent
    # "no budget": the limit that is missing at the moment of the order is the one
    # that mattered.
    try:
        risk_budget = RiskBudget.load(RISK_BUDGET)
    except RiskBudgetError as exc:
        print(f"{exc}\nrefusing to arm: a risk budget that cannot be read is not "
              f"the same as no risk budget", file=sys.stderr)
        return 1

    live = bool(args.live)
    if live and not os.environ.get("MFF_RH_TOKEN"):
        print("--live needs MFF_RH_TOKEN", file=sys.stderr)
        return 1
    quotes = HttpQuoteProvider()
    if live and not quotes.configured:
        print("--live needs a quote feed: set MFF_QUOTE_URL_TEMPLATE (see doctor). "
              "Without it every entry would fail closed with no_quote - correct but useless.",
              file=sys.stderr)
        return 1
    broker = RobinhoodAgenticBroker(dry_run=not live)
    listener = Listener(
        transport=WebsocketGateway(token=token),
        engine=Engine(config=EngineConfig(), fills=FillLog(path=FILL_LOG, fsync=False),
                      governor=make_governor(risk_policy),
                      risk_budget=risk_budget,
                      risk_state=risk_state, risk_state_path=state_path),
        broker=broker,
        quotes=quotes,
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=HEARTBEAT),
        kill=kill,
    )
    print(f"armed | broker={'LIVE' if live else 'DRY'} | "
          f"quotes={'configured' if quotes.configured else 'NOT configured (entries will reject)'} | "
          f"channels={','.join(CHANNEL_IDS)}")
    print(f"risk  | policy {risk_policy.name} (1R=${risk_policy.risk_unit_usd:,.0f}, "
          f"daily stop ${risk_policy.daily_loss_limit_usd_effective() or 0:,.0f}) | "
          f"equity ${risk_state.equity_usd:,.2f} | peak ${risk_state.peak_equity_usd:,.2f}")
    if risk_budget is not None:
        print(f"budget| total ${risk_budget.max_total_premium_usd or 0:,.0f} | "
              f"underlying ${risk_budget.max_underlying_premium_usd or 0:,.0f} | "
              f"cluster ${risk_budget.max_cluster_premium_usd or 0:,.0f} | "
              f"worst-case day ${risk_budget.daily_loss_limit_usd or 0:,.0f}")
    else:
        print("budget| NONE - open premium is not capped before the order "
              f"(write {RISK_BUDGET.name} to change that)")

    async def pump() -> int:
        count = 0
        async for message in listener.transport.messages():
            intents = listener.handle(message)
            kind = "trade" if intents else "none"
            print(f"  {message.ts:>10.3f} {message.channel:<24} {kind:<6} "
                  f"{len(intents)} intent(s) latency={listener.budget.worst_ms}ms")
            count += 1
            if args.limit is not None and count >= args.limit:
                break
        return count

    try:
        asyncio.run(pump())
    except KeyboardInterrupt:
        print("interrupted")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    kill = KillSwitch(path=NO_TRADE)
    print(f"kill switch: {kill.status()}")
    if not FILL_LOG.exists():
        print(f"no fill log yet at {FILL_LOG}")
        return 0
    rows = FillLog().replay_file(FILL_LOG)
    print(f"fill log: {len(rows)} rows, seq {rows[0].seq}..{rows[-1].seq}")
    for fill in rows[-15:]:
        print(f"  {fill.seq:>5} {fill.event:<8} {fill.qty:>3}x {fill.contract_key:<28} "
              f"{fill.price:<8.2f} {fill.reason}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from . import historic

    kwargs = dict(ask_multiple=args.ask_multiple, exit_slippage=args.exit_slippage,
                  pipe_test=not args.no_pipe_test, named_exits=not args.bare_exits)
    if args.corpus is not None:
        kwargs["corpus"] = args.corpus
    report = historic.run_backtest(**kwargs)

    if args.md is not None:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(report.to_markdown(), encoding="utf-8")

    if args.json:
        print(json.dumps({
            "corpus": str(args.corpus or historic.DEFAULT_CORPUS),
            "alerts": report.trades, "days": report.days,
            "taken": report.taken, "skipped": report.skipped, "rejected": report.rejected,
            "contracts": report.contracts, "notional_usd": report.notional_usd,
            "pnl_usd": report.pnl_usd, "avg_pnl_usd": round(report.avg_pnl_usd, 2),
            "win_rate_pct": round(report.win_rate, 2),
            "by_channel": report.by_channel, "skip_reasons": report.skip_reasons,
            "assumptions": {"ask_multiple": report.ask_multiple,
                            "exit_slippage": report.exit_slippage,
                            "pipe_test": report.pipe_test},
        }, indent=2))
        return 0

    print(report.to_text())
    if args.md is not None:
        print(f"\nmarkdown report -> {args.md}")

    if args.attribution:
        print("\nwhat each locked rule is worth (one rule changed at a time):")
        base = None
        for label, variant in historic.attribution(corpus=args.corpus or historic.DEFAULT_CORPUS):
            if base is None:
                base = variant.pnl_usd
            print(f"  {label:<44} taken {variant.taken:>3}  contracts {variant.contracts:>4}  "
                  f"pnl ${variant.pnl_usd:>11,.2f}  vs locked {variant.pnl_usd - base:+,.2f}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from . import watch as watch_mod

    kwargs = {"dry_run": args.dry_run}
    if args.stale_after is not None:
        kwargs["stale_after_s"] = args.stale_after
    if args.heartbeat is not None:
        kwargs["heartbeat_path"] = args.heartbeat
    if args.fills is not None:
        kwargs["fills_path"] = args.fills
    if args.kill is not None:
        kwargs["kill_path"] = args.kill

    health, result = watch_mod.run_watch(**kwargs)
    if args.json:
        payload = health.to_dict()
        payload["page"] = result.to_dict() if result is not None else None
        print(json.dumps(payload, indent=2))
    else:
        print(f"watch: {health.status.upper()}")
        for problem in health.problems:
            print(f"  {problem}")
        if result is None:
            print("  nothing to page")
        elif result.dry_run:
            print("\n--- page that would be sent ---")
            print(health.page_text())
        elif result.ok:
            print(f"  paged: {result.chunks} message(s), ids {list(result.message_ids)}")
        else:
            print(f"  PAGE FAILED: {result.error}")
    if health.healthy:
        return 0
    if result is not None and result.dry_run:
        return 1
    return 1 if (result is not None and result.ok) else 2


def cmd_policy(args: argparse.Namespace) -> int:
    from . import policy as policy_mod

    path = args.path or policy_mod.DEFAULT_POLICY_PATH
    if args.policy_cmd == "init":
        if Path(path).exists():
            print(f"refusing to overwrite {path} - a risk policy is not a generated file")
            return 2
        written = policy_mod.write(policy_mod.baseline(args.name), path)
        print(f"wrote baseline policy -> {written}")
        print()
        print(policy_mod.summarise(policy_mod.load(written)))
        return 0

    if args.policy_cmd == "simulate":
        from . import historic
        try:
            policy = policy_mod.load(path)
        except policy_mod.PolicyError as exc:
            print(exc)
            return 2
        base = historic.run_backtest(ask_multiple=args.ask_multiple,
                                     exit_slippage=args.exit_slippage)
        governed, decisions = historic.run_governed(policy,
                                                    ask_multiple=args.ask_multiple,
                                                    exit_slippage=args.exit_slippage)
        if args.json:
            print(json.dumps({
                "policy": policy.name,
                "ungoverned": {"taken": base.taken, "contracts": base.contracts,
                               "pnl_usd": base.pnl_usd},
                "governed": {"taken": governed.taken, "contracts": governed.contracts,
                             "pnl_usd": governed.pnl_usd,
                             "skip_reasons": governed.skip_reasons},
                "interventions": [{"date": t.date, "channel": t.channel, "factor": f,
                                   "reason": r} for t, f, r in decisions],
            }, indent=2))
            return 0
        print(f"policy {policy.name} over the corpus")
        print(f"  ungoverned : taken {base.taken:>3}  contracts {base.contracts:>4}  "
              f"pnl ${base.pnl_usd:>11,.2f}")
        print(f"  governed   : taken {governed.taken:>3}  contracts {governed.contracts:>4}  "
              f"pnl ${governed.pnl_usd:>11,.2f}")
        delta = governed.pnl_usd - base.pnl_usd
        print(f"  difference : ${delta:+,.2f}  "
              f"({len(decisions)} entries touched by the policy)")
        if decisions:
            print("\n  interventions:")
            for trade, factor, reason in decisions[:20]:
                print(f"    {trade.date} {trade.time_et} {trade.channel:<22} "
                      f"x{factor:<4g} {reason}")
            if len(decisions) > 20:
                print(f"    ... and {len(decisions) - 20} more")
        if governed.skip_reasons:
            print("\n  governed skips:", ", ".join(
                f"{k}={v}" for k, v in sorted(governed.skip_reasons.items())))
        return 0

    # show / validate
    try:
        policy = policy_mod.load(path)
    except policy_mod.PolicyError as exc:
        print(exc)
        return 2
    if args.json:
        print(json.dumps(policy.to_dict(), indent=2))
        return 0
    print(policy_mod.summarise(policy))
    if args.policy_cmd == "validate":
        print("\n  valid: parses, every multiplier within the locked cap, reduce-only\n"
              "  note: freshness is checked at arm time, not here - a stale policy "
              "still parses")
    return 0


def cmd_desk(args: argparse.Namespace) -> int:
    """The statistics layer: what the record says, and where the next dollar belongs."""
    from . import expectancy, historic
    from . import policy as policy_mod

    risk_unit = args.risk_unit
    if risk_unit is None:
        try:
            risk_unit = policy_mod.load().risk_unit_usd
        except policy_mod.PolicyError:
            risk_unit = policy_mod.DEFAULT_R_USD

    report = historic.run_backtest()
    rows = historic.outcomes_from(report)
    desk = expectancy.report(rows, risk_unit_usd=risk_unit)

    if args.md is not None:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(desk.to_markdown(), encoding="utf-8")

    if args.json:
        print(json.dumps({
            "overall": desk.overall.to_dict(),
            "channels": {name: stats.to_dict() for name, stats in desk.channels.items()},
            "ranking": [{"channel": name, "score": round(score, 4)} for name, score, _ in desk.ranking],
            "drawdown": desk.dd.to_dict(),
            "runs_test": desk.runs,
            "streak_expectations": [{"length": n, "expected": round(v, 2)}
                                    for n, v in desk.streak_table],
            "risk_unit_usd": desk.risk_unit_usd,
        }, indent=2))
        return 0

    print(desk.to_text())
    if args.md is not None:
        print(f"\nmarkdown report -> {args.md}")
    return 0


def cmd_tca(args: argparse.Namespace) -> int:
    """Execution quality. `path` needs bars (here); `fills` needs venue executions (live)."""
    from . import historic, tca as tca_mod

    if args.tca_cmd == "fills":
        path = args.executions or (ROOT / "live" / "executions.jsonl")
        log = tca_mod.ExecutionLog.load(path)
        report = tca_mod.FillReport(executions=log.records)
        if args.json:
            print(json.dumps({"executions": len(log.records),
                              "mean_cost_bps": round(report.mean_cost_bps, 1),
                              "p90_cost_bps": round(report.p90_cost_bps, 1),
                              "total_cost_usd": report.total_cost_usd,
                              "at_or_better_pct": round(report.at_or_better_share, 1),
                              "partial_pct": round(report.partial_share, 1),
                              "latency_p90_ms": round(report.latency_p90_ms, 1),
                              "suggested_ask_multiple": report.suggested_ask_multiple(),
                              "source": str(path)}, indent=2))
            return 0
        print(report.to_text())
        if not log.records:
            print(f"  (looked in {path})")
        return 0

    bars_dir = args.bars or tca_mod.DEFAULT_BARS_DIR
    barsets = tca_mod.load_barsets(bars_dir)
    if not barsets:
        print(f"no bar series under {bars_dir} - nothing to analyse", file=sys.stderr)
        return 2
    report = tca_mod.analyse_paths(barsets, historic.load_trades(),
                                   ask_multiple=args.ask_multiple)
    if args.md is not None:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(report.to_markdown(), encoding="utf-8")
    if args.json:
        print(json.dumps({
            "barsets": report.barsets, "joined": report.n,
            "measurable": len(report.measurable),
            "entry_before_coverage": report.n - len(report.in_coverage),
            "aggregates_withheld": not report.measurable_enough,
            "mae_median_pct": round(report.mae_median, 2) if report.measurable_enough else None,
            "mfe_median_pct": round(report.mfe_median, 2) if report.measurable_enough else None,
            "stop30_hit_pct": round(report.stop30_share, 1) if report.measurable_enough else None,
            "carry_median_pct": round(report.carry_median, 2),
            "expired_pct": round(report.expired_share, 1),
            "unmatched": report.unmatched,
            "rows": [r.to_dict() for r in report.rows],
        }, indent=2))
        return 0
    print(report.to_text())
    if args.md is not None:
        print(f"\nmarkdown report -> {args.md}")
    return 0


def cmd_fuzz(args: argparse.Namespace) -> int:
    """Exit 1 on a violation, so CI can gate on it."""
    from . import fuzz

    campaign = fuzz.run_campaign(seed=args.seed, iterations=args.iterations)
    if args.json:
        print(json.dumps({"seed": args.seed, "iterations": args.iterations,
                          "posts": campaign.posts, "intents": campaign.intents,
                          "clean": campaign.clean,
                          "violations": [{"invariant": v.invariant, "detail": v.detail,
                                          "post": v.post} for v in campaign.violations],
                          "statuses": campaign.statuses}, indent=2))
    else:
        print(campaign.to_text())
    return 0 if campaign.clean else 1


def cmd_risk_budget(args: argparse.Namespace) -> int:
    path = args.path or RISK_BUDGET
    if args.risk_budget_cmd == "init":
        if path.exists():
            print(f"{path} already exists - review it, do not overwrite a risk limit",
                  file=sys.stderr)
            return 1
        # The defaults are a shape, not a recommendation: total = 2R, one underlying
        # = 2/3 of that, one cluster = 5/6. Review before arming.
        budget = RiskBudget(
            max_total_premium_usd=args.total,
            max_underlying_premium_usd=round(args.total * 2 / 3, 2),
            max_cluster_premium_usd=round(args.total * 5 / 6, 2),
            daily_loss_limit_usd=args.daily,
        )
        budget.save(path)
        print(f"wrote {path}")
        print(f"  total ${budget.max_total_premium_usd:,.2f} | underlying "
              f"${budget.max_underlying_premium_usd:,.2f} | cluster "
              f"${budget.max_cluster_premium_usd:,.2f} | daily "
              f"${budget.daily_loss_limit_usd:,.2f}")
        print("review the numbers before arming; the file is reduce-only")
        return 0

    try:
        budget = RiskBudget.load(path, missing_ok=False)
    except RiskBudgetError as exc:
        print(f"risk budget invalid: {exc}", file=sys.stderr)
        return 1
    assert budget is not None
    if args.json:
        print(json.dumps({"path": str(path), **budget.to_dict()}, indent=2))
        return 0
    print(f"risk budget | {path}")
    for name, value in budget.caps().items():
        print(f"  {name:<28} ${value:,.2f}")
    print(f"  {'daily_loss_limit_usd':<28} "
          f"{'$' + format(budget.daily_loss_limit_usd, ',.2f') if budget.daily_loss_limit_usd else 'none'}")
    print(f"  clusters                     {len(budget.clusters)}")
    for group in budget.clusters:
        print(f"    {', '.join(sorted(group))}")
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    if args.evidence_cmd != "report":
        return 2
    if args.corpus is not None:
        trades, meta = load_corpus(args.corpus)
        source = SOURCE_CORPUS
    else:
        trades, meta = load_fills(args.fills or FILL_LOG)
        source = "LIVE_FILLS"
    if not trades:
        print("no closed trades to measure", file=sys.stderr)
        if meta.get("anomalies"):
            print(f"  anomalies: {meta['anomalies']}", file=sys.stderr)
        return 1
    report = build_report(trades, source=source, draws=args.draws, seed=args.seed)
    attribution = None
    context_error = ""
    if args.context is not None:
        try:
            attribution = attribute_context(trades, load_context(args.context))
        except ContextError as exc:
            # Optional context: a bad snapshot is ignored with a warning. It must
            # never turn a research report into a failure, and it must never gate.
            context_error = str(exc)
    if args.json:
        payload = {"report": report.to_dict(), "input": meta,
                   "context": attribution.to_dict() if attribution else None,
                   "context_error": context_error or None}
        print(json.dumps(payload, indent=2))
    else:
        print(to_text(report))
        if meta.get("anomalies") and any(meta["anomalies"].values()):
            print(f"  input anomalies: {meta['anomalies']}")
        if meta.get("open_positions"):
            print(f"  open (not counted): {meta['open_positions']} positions, "
                  f"${meta.get('open_premium_usd', 0):,.2f} premium")
        if attribution is not None:
            print()
            print(attribution_text(attribution))
        elif context_error:
            print(f"  context ignored: {context_error}")
    if args.md is not None:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        body = to_markdown(report)
        if attribution is not None:
            body += ("\n## Scanner context (attribution only)\n\n```\n"
                     + attribution_text(attribution) + "\n```\n")
        args.md.write_text(body, encoding="utf-8")
        if not args.json:
            print(f"  wrote {args.md}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers = {
        "doctor": cmd_doctor,
        "replay": cmd_replay,
        "arm": cmd_arm,
        "status": cmd_status,
        "backtest": cmd_backtest,
        "watch": cmd_watch,
        "policy": cmd_policy,
        "desk": cmd_desk,
        "tca": cmd_tca,
        "fuzz": cmd_fuzz,
        "risk-budget": cmd_risk_budget,
        "evidence": cmd_evidence,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

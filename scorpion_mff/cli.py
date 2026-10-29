"""Operator CLI: doctor / replay / arm / status.

    python -m scorpion_mff.cli doctor
    python -m scorpion_mff.cli replay tapes/sep8.json
    python -m scorpion_mff.cli arm            # dry-armed (no orders leave the box)
    python -m scorpion_mff.cli arm --live     # needs MFF_RH_TOKEN, explicit opt-in
    python -m scorpion_mff.cli status
    python -m scorpion_mff.cli verify           # fill-log integrity + unresolved intents
    python -m scorpion_mff.cli recover          # resolve crash-left intents (never places)
    python -m scorpion_mff.cli book show        # the confirmed book (acks, partials, digest)
    python -m scorpion_mff.cli flatten          # plan: sell every confirmed lot at the bid
    python -m scorpion_mff.cli flatten --live   # the operator's exit (needs a quote feed)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .broker import (
    HttpQuoteProvider,
    OrderLookup,
    PaperBroker,
    QuoteProvider,
    RobinhoodAgenticBroker,
    mask,
)
from .engine import Engine, EngineConfig
from .fills import FillLog
from .gateway import GatewayFatalError
from .health import Heartbeat, LatencyBudget
from .killswitch import KillSwitch
from .ledger import Ledger
from .listener import Listener, TapeTransport, WebsocketGateway
from .parser import CHANNEL_IDS, EXCLUDED_CHANNEL_IDS, EXCLUDED_TICKERS
from .replay import load_quotes, load_tape, replay
from .types import Fill, RawMessage

if TYPE_CHECKING:                       # `parity` imports the listener; keep it out of
    from .parity import ParityReport  # the module import graph at runtime

ROOT = Path(__file__).resolve().parent.parent
FILL_LOG = ROOT / "live" / "fills.jsonl"
# The confirmed-execution log: intent -> venue ack -> fill, folded into a durable book.
# Machine state, like the fill log, and never committed (see .gitignore).
EXECUTION_LOG = ROOT / "live" / "execution.jsonl"
# One tca.Execution row per filled order: the shape `tca fills` measures.
EXECUTIONS_LOG = ROOT / "live" / "executions.jsonl"
HEARTBEAT = ROOT / "live" / "heartbeat.json"
NO_TRADE = ROOT / "live" / "NO_TRADE"

__version__ = "0.1.0"


def _spread_gate_text() -> str:
    """Human-readable spread-gate state, shared by doctor and arm."""
    ratio = EngineConfig().max_spread_ratio
    if ratio is None:
        return "OFF (MFF_MAX_SPREAD_RATIO unset)"
    return f"ON (max {ratio:.1%})"


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
    arm.add_argument("--enforce-breakeven", action="store_true",
                     help="poll quotes and flatten any position at/through its BE stop")
    arm.add_argument("--no-expiry-guard", action="store_true",
                     help="DISARM the expiry guard (default: on, see 'expiry audit')")
    arm.add_argument("--expiry-cutoff", type=int, default=15, metavar="MINUTES",
                     help="flatten window before the close (default 15 -> 15:45 ET)")
    arm.add_argument("--tick-s", type=float, default=15.0,
                     help="clock cadence of the no-traffic tick (default 15s)")
    arm.add_argument("--max-quote-age", type=float, default=5.0,
                     help="refuse a quote older than this many seconds (default 5)")
    arm.add_argument("--allow-unresolved", action="store_true",
                     help="arm even when the confirmed book has an order with no venue "
                          "answer (default: refuse until `recover` settles it)")
    arm.add_argument("--venue-snapshot", type=Path, default=None,
                     help="reconcile against this venue positions file every --venue-every "
                          "seconds (overrides MFF_VENUE_SNAPSHOT/MFF_VENUE_URL)")
    arm.add_argument("--venue-every", type=float, default=300.0, metavar="SECONDS",
                     help="scheduled reconcile cadence (default 300s)")
    arm.add_argument("--venue-max-age", type=float, default=120.0, metavar="SECONDS",
                     help="refuse a venue snapshot older than this (default 120s)")
    arm.add_argument("--trace", type=Path, default=None,
                     help="record one decision trace per event (JSON) so the live session "
                          "can be diffed against the offline replay with `shadow --diff`")

    sub.add_parser("status", help="kill-switch state + tail of the fill log")

    ver = sub.add_parser("verify", help="validate the fill log for gaps, duplicates, torn rows")
    ver.add_argument("--fills", type=Path, default=None,
                     help="fill log (default live/fills.jsonl)")

    rec = sub.add_parser("recover",
                         help="resolve crash-left intents from a broker lookup; never places")
    rec.add_argument("--fills", type=Path, default=None,
                     help="fill log (default live/fills.jsonl)")
    rec.add_argument("--broker", choices=("robinhood", "paper"), default="robinhood",
                     help="lookup backend (default robinhood; paper is deterministic)")

    fl = sub.add_parser("flatten",
                        help="sell every confirmed lot at the live bid; dry run unless --live")
    fl.add_argument("--live", action="store_true",
                    help="place the sells (default: print the plan and send nothing)")
    fl.add_argument("--json", action="store_true", help="machine-readable report")

    marks = sub.add_parser("marks", help="mark the open book to market (unrealised P&L)")
    marks.add_argument("--fills", type=Path, default=None,
                       help="fill log (default live/fills.jsonl)")
    marks.add_argument("--quotes", type=Path, default=None,
                       help="JSON quotes file; else the live quote feed when configured")
    marks.add_argument("--json", action="store_true", help="machine-readable marks")
    marks.add_argument("--check", action="store_true",
                       help="exit 1 when any armed breakeven stop is through, "
                            "or the book cannot be priced")

    bt = sub.add_parser("backtest", help="run the executor over the historical alert corpus")
    bt.add_argument("--corpus", type=Path, default=None,
                    help="trades.json (default: mff-backtest/bt_full)")
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
    desk.add_argument("--risk-unit", type=float, default=None,
                      help="1R in USD (default: policy, else $1500)")
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

    exp = sub.add_parser("exposure", help="correlated-book cap: init the limits, show the book")
    exp_sub = exp.add_subparsers(dest="exposure_cmd", required=True)
    e_init = exp_sub.add_parser("init", help="write default limits (refuses to overwrite)")
    e_init.add_argument("--path", type=Path, default=None)
    e_show = exp_sub.add_parser("show",
                                help="rebuild the book from the fill log and show concentration")
    e_show.add_argument("--path", type=Path, default=None,
                        help="limits file (default live/exposure.json)")
    e_show.add_argument("--fills", type=Path, default=None,
                        help="fill log (default live/fills.jsonl)")
    e_show.add_argument("--json", action="store_true")

    rk = sub.add_parser("risk", help="pre-trade risk budget: worst-case loss before the order")
    rk_sub = rk.add_subparsers(dest="risk_cmd", required=True)
    rk_init = rk_sub.add_parser("init", help="write the reviewed starter budget (never overwrites)")
    rk_init.add_argument("--path", type=Path, default=None)
    rk_show = rk_sub.add_parser("show", help="usage against every cap, rebuilt from the fills")
    rk_show.add_argument("--path", type=Path, default=None,
                         help="budget file (default live/risk-budget.json)")
    rk_show.add_argument("--fills", type=Path, default=None,
                         help="fill log (default live/fills.jsonl)")
    rk_show.add_argument("--realized-today", type=float, default=None,
                         help="override the realised P&L read from live/governor.json")
    rk_show.add_argument("--json", action="store_true")
    rk_val = rk_sub.add_parser("validate", help="parse every cap and print what binds")
    rk_val.add_argument("--path", type=Path, default=None)
    rk_val.add_argument("--json", action="store_true")

    rc = sub.add_parser("reconcile",
                        help="compare the local book with a venue snapshot; drift exits 1")
    rc.add_argument("--snapshot", type=Path, required=True,
                    help="JSON array of {contract_key, contracts}")
    rc.add_argument("--fills", type=Path, default=None,
                    help="fill log (default live/fills.jsonl)")
    rc.add_argument("--source", choices=("auto", "confirmed", "fills"), default="auto",
                    help="which local book to compare: the confirmed execution log, "
                         "the optimistic fill log, or auto (confirmed when it has orders)")
    rc.add_argument("--max-age", type=float, default=None, metavar="SECONDS",
                    help="refuse a snapshot older than this (default: no age check; "
                         "the scheduled path in `arm` defaults to 120s)")
    rc.add_argument("--json", action="store_true")

    bk = sub.add_parser("book",
                        help="confirmed execution book: what the venue answered, replayable")
    bk_sub = bk.add_subparsers(dest="book_cmd", required=True)
    bk_show = bk_sub.add_parser("show", help="the confirmed book, replayed from its log")
    bk_show.add_argument("--path", type=Path, default=None,
                         help="execution log (default live/execution.jsonl)")
    bk_show.add_argument("--json", action="store_true")
    bk_replay = bk_sub.add_parser(
        "replay", help="replay the log twice and prove the two books are identical")
    bk_replay.add_argument("--path", type=Path, default=None)
    bk_replay.add_argument("--json", action="store_true")
    bk_verify = bk_sub.add_parser(
        "verify", help="structural defects in the log; --strict also fails on unresolved")
    bk_verify.add_argument("--path", type=Path, default=None)
    bk_verify.add_argument("--strict", action="store_true",
                           help="exit 1 when an order has no venue answer (cron mode)")
    bk_verify.add_argument("--json", action="store_true")
    bk_sync = bk_sub.add_parser(
        "sync", help="rebuild a fresh engine from confirmed state (the restart proof)")
    bk_sync.add_argument("--path", type=Path, default=None)
    bk_sync.add_argument("--now", type=float, default=None,
                         help="epoch seconds to restore at (default: now)")
    bk_sync.add_argument("--json", action="store_true")
    bk_export = bk_sub.add_parser(
        "export", help="write confirmed fills as tca executions, so `tca fills` prices real fills")
    bk_export.add_argument("--path", type=Path, default=None)
    bk_export.add_argument("--executions", type=Path, default=None,
                           help="target file (default live/executions.jsonl)")
    bk_export.add_argument("--replace", action="store_true",
                           help="overwrite an existing executions file")
    bk_export.add_argument("--json", action="store_true")

    xp = sub.add_parser("expiry", help="expiry guard: audit the corpus against the close cutoff")
    xp_sub = xp.add_subparsers(dest="expiry_cmd", required=True)
    xa = xp_sub.add_parser("audit", help="how many alerts are 0DTE, how many never closed, "
                                          "and what the cutoff would have caught")
    xa.add_argument("--corpus", type=Path, default=None,
                    help="trades.json (default: mff-backtest/bt_full)")
    xa.add_argument("--cutoff-minutes", type=int, default=15,
                    help="flatten window before the close (default 15 -> 15:45 ET)")
    xa.add_argument("--json", action="store_true", help="machine-readable audit")

    sh = sub.add_parser(
        "shadow",
        help="run one tape through the decision wiring and the live wiring, then compare")
    sh.add_argument("tape", type=Path, nargs="?", default=None,
                    help="recorded tape (messages + quotes), e.g. tapes/sep8.json")
    sh.add_argument("--diff", nargs=2, metavar=("REFERENCE", "CANDIDATE"), default=None,
                    help="compare two recorded traces instead of running a tape")
    sh.add_argument("--trace-dir", type=Path, default=None,
                    help="write replay.json and shadow.json here (a manifest a reviewer runs)")
    sh.add_argument("--no-pipe-test", action="store_true",
                    help="size the first trade at full clip on both wirings")
    sh.add_argument("--venue-fill", type=float, default=1.0, metavar="FRACTION",
                    help="shadow venue fill fraction (default 1.0; <1 models a partial)")
    sh.add_argument("--venue-reject", default="", metavar="REASON",
                    help="shadow venue refuses every order with this reason")
    sh.add_argument("--venue-transport", default="", metavar="REASON",
                    help="shadow venue does not answer; the order must stay unknown")
    sh.add_argument("--json", action="store_true", help="machine-readable parity report")
    return p


def cmd_doctor(args: argparse.Namespace) -> int:
    kill = KillSwitch(path=NO_TRADE)
    hb = Heartbeat(path=HEARTBEAT)
    if os.environ.get("DISCORD_BOT_TOKEN"):
        discord_token = "present"
    else:
        discord_token = "MISSING (arm will refuse)"
    broker_mode = "DRY (no orders leave the box)" if not os.environ.get("MFF_RH_LIVE") else "LIVE"
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
        f"discord token               {discord_token}",
        f"robinhood token             {mask(os.environ.get('MFF_RH_TOKEN', ''))}",
        f"robinhood base              {os.environ.get('MFF_RH_BASE', 'https://api.robinhood.com')}",
        f"broker mode                 {broker_mode}",
        f"spread gate                 {_spread_gate_text()}",
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


async def pump_listener(listener: Listener, limit: int | None = None,
                        out: Callable[[str], None] = print) -> int:
    """Messages as they arrive, plus a tick whenever the channel goes quiet.

    `asyncio.wait_for(anext(...), tick_s)` is what gives the expiry guard a clock: nothing
    else fires at 15:45 ET if nobody posts. It is wrapped in `shield` deliberately, and
    that is not decoration. A bare `wait_for` CANCELS `anext` on timeout, and cancelling an
    async generator's `__anext__` closes the generator - the Gateway connection is torn
    down, the pump exits, and the bot goes silent on the first quiet quarter hour of the
    session. That is the 9/8 failure with extra steps. So the pending fetch is shielded
    and merely re-awaited: the timeout fires the tick, the in-flight fetch survives, and no
    message can be swallowed by a tick.

    Returns the number of messages handled. `--limit` counts messages only, never ticks,
    so a bounded run cannot be starved by an idle channel.
    """
    count = 0
    ticks = 0
    messages = listener.transport.messages()
    pending: asyncio.Future[RawMessage] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(messages))
            try:
                message = await asyncio.wait_for(asyncio.shield(pending), listener.tick_s)
                pending = None
            except TimeoutError:
                ticks += 1
                clock = listener.clock()
                if ticks == 1 or ticks % 20 == 0:
                    out(f"  {clock:>10.3f} {'tick':<24} expiry-guard      "
                        f"{len(listener.engine.open_positions)} open "
                        f"{listener.engine.stats.expiry_pages} page(s)")
                listener.tick(clock)
                continue
            except StopAsyncIteration:
                pending = None
                break
            intents = listener.handle(message)
            stops = listener.refresh_stops()
            kind = "trade" if intents else "none"
            out(f"  {message.ts:>10.3f} {message.channel:<24} {kind:<6} "
                f"{len(intents)} intent(s) {len(stops)} stop(s) "
                f"latency={listener.budget.worst_ms}ms")
            count += 1
            if limit is not None and count >= limit:
                break
    finally:
        if pending is not None:
            pending.cancel()
    return count


def cmd_arm(args: argparse.Namespace) -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("DISCORD_BOT_TOKEN is not set - refusing to arm", file=sys.stderr)
        return 1
    kill = KillSwitch(path=NO_TRADE)
    if kill.active():
        print(f"kill switch is active: {kill.status()}  "
              f"(delete {kill.path} to arm)", file=sys.stderr)
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
    from . import exposure as exposure_mod
    from . import risk as risk_mod
    exposure_path = ROOT / "live" / "exposure.json"
    exposure_limits = None
    if exposure_path.exists():
        try:
            exposure_limits = exposure_mod.load_limits(exposure_path)
        except (OSError, ValueError) as exc:
            print(f"refusing to arm: invalid exposure limits at {exposure_path}: {exc}",
                  file=sys.stderr)
            return 1

    # The pre-trade budget is opt-in the same way exposure is: the file's presence
    # activates it. An unreadable or contradictory budget refuses the arm rather than
    # falling back to "no cap", because silence here would be an unlimited worst case.
    risk_budget_path = ROOT / "live" / "risk-budget.json"
    risk_budget = None
    if risk_budget_path.exists():
        try:
            risk_budget = risk_mod.RiskBudget.load(risk_budget_path, missing_ok=False)
        except (OSError, ValueError) as exc:
            print(f"refusing to arm: invalid risk budget at {risk_budget_path}: {exc}",
                  file=sys.stderr)
            return 1

    broker = RobinhoodAgenticBroker(dry_run=not live)
    expiry_guard = not bool(args.no_expiry_guard)
    gateway = WebsocketGateway(token=token)
    engine = Engine(
        config=EngineConfig(enforce_breakeven=bool(args.enforce_breakeven),
                            enforce_expiry=expiry_guard,
                            expiry_cutoff_minutes=int(args.expiry_cutoff),
                            max_quote_age_s=args.max_quote_age),
        fills=FillLog(path=FILL_LOG, fsync=False),
        governor=make_governor(risk_policy),
        risk_budget=risk_budget,
        exposure_limits=exposure_limits,
        risk_state=risk_state, risk_state_path=state_path)

    # The confirmed book is restored BEFORE the first message is handled. The engine's
    # positions are a decision cache; this is the state the venue actually confirmed,
    # so a restart resumes holding what it holds instead of an empty book - and it
    # cannot forget a realised drawdown, because the book carries the baseline.
    from .execution_book import ExecutionBook, ExecutionBookError
    from .venue import FileVenueFeed, feed_from_env

    execution_path = EXECUTION_LOG
    try:
        execution = ExecutionBook.load(execution_path, day_of=engine.trading_day)
    except ExecutionBookError as exc:
        print(f"refusing to arm: the confirmed execution log at {execution_path} cannot "
              f"be trusted: {exc}", file=sys.stderr)
        return 1
    execution.ensure_baseline(
        equity_usd=risk_state.equity_usd, peak_equity_usd=risk_state.peak_equity_usd,
        day=risk_state.day, realized_today_usd=risk_state.realized_today_usd,
        trades_today=risk_state.trades_today,
        consecutive_losses=risk_state.consecutive_losses, ts=time.time())
    restored = execution.sync_engine(engine, time.time())
    if restored.unresolved and not args.allow_unresolved:
        # An order with no venue answer means the book is a hypothesis. Arming on a
        # hypothesis is how a restart becomes a double entry, so this is a refusal:
        # `recover` settles it from a lookup, and `--allow-unresolved` is the explicit
        # override for an operator who has checked by hand.
        print(f"refusing to arm: {restored.unresolved} order(s) have no venue answer "
              f"(run `book verify` then `recover`) - pass --allow-unresolved to override",
              file=sys.stderr)
        for order in execution.unresolved_orders():
            print(f"  {order.state:<8} {order.action:<4} {order.contract_key:<28} "
                  f"{order.filled_qty}/{order.intent.qty} {order.client_id}", file=sys.stderr)
        return 1

    # Scheduled venue reconciliation. A snapshot the venue published is evidence; the
    # engine halts NEW buys on drift and keeps exits legal. A feed that cannot be read is
    # a page, never a halt - a broken adapter must not become a stopped desk.
    venue_feed = (FileVenueFeed(path=args.venue_snapshot, max_age_s=args.venue_max_age)
                  if args.venue_snapshot is not None
                  else feed_from_env(max_age_s=args.venue_max_age))

    listener = Listener(
        transport=gateway,
        engine=engine,
        broker=broker,
        quotes=quotes,
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=HEARTBEAT),
        kill=kill,
        execution=execution,
        tick_s=float(args.tick_s),
        venue_feed=venue_feed,
        venue_every_s=float(args.venue_every),
    )
    if args.trace is not None:
        # A live session's decisions become an artefact. The recorder is attached to the
        # same `handle` the trade path runs, so the trace cannot describe a different code
        # path than the one that decided - and `shadow --diff` can compare last Tuesday's
        # session with today's replay without replaying the session itself.
        from .parity import Trace as TraceMod
        listener.trace = TraceMod(mode="live", tape="gateway")

    def record_gateway_alert(reason: str, note: str) -> None:
        # A reconnect is routine; a gap, zombie or fatal close is a decision-level
        # event. All of them land in the same fill log as orders, so `watch` can
        # page on a transport failure without a second observability system.
        listener.engine.fills.record(
            event="alert", contract_key="", qty=0, price=0.0, reason=reason,
            accepted=False, ts=time.time(), note=note,
        )

    gateway.alert = record_gateway_alert
    quotes_status = "configured" if quotes.configured else "NOT configured (entries will reject)"
    print(f"armed | broker={'LIVE' if live else 'DRY'} | "
          f"quotes={quotes_status} | "
          f"channels={','.join(CHANNEL_IDS)}")
    guard = (f"ON (flatten at {args.expiry_cutoff}m before the ET close, tick "
             f"{args.tick_s:g}s)" if expiry_guard else "OFF (--no-expiry-guard)")
    print(f"expiry | guard {guard} | backtest parity unaffected (this is the live path)")
    print(f"risk  | policy {risk_policy.name} (1R=${risk_policy.risk_unit_usd:,.0f}, "
          f"daily stop ${risk_policy.daily_loss_limit_usd_effective() or 0:,.0f}) | "
          f"equity ${risk_state.equity_usd:,.2f} | peak ${risk_state.peak_equity_usd:,.2f}")
    print(f"spread | gate {_spread_gate_text()}")
    print(f"book  | {restored.positions} confirmed position(s) / {restored.contracts} "
          f"contract(s) | realised ${restored.realized_usd:,.2f} | "
          f"{restored.unresolved} unresolved | digest {execution.digest()[:16]}")
    if restored.expired:
        print(f"       {restored.expired} carried lot(s) expired before this arm and were "
              f"released (marked at zero; see `book show`)")
    if restored.unresolved:
        print(f"       armed with {restored.unresolved} unresolved order(s) "
              f"(--allow-unresolved): the venue state for them is still unknown")
    if venue_feed is None:
        print("venue | reconcile feed NOT configured - drift detection is OFF "
              "(set MFF_VENUE_URL or pass --venue-snapshot)")
    else:
        print(f"venue | reconcile every {args.venue_every:g}s against {venue_feed.source} | "
              f"snapshot age limit {args.venue_max_age:g}s | drift halts buys, exits stay legal")
    if risk_budget is None:
        print("budget | OFF (no live/risk-budget.json; worst-case day is unbounded)")
    else:
        print(f"budget | ON total ${risk_budget.max_total_premium_usd or 0:,.0f} | "
              f"underlying ${risk_budget.max_underlying_premium_usd or 0:,.0f} | "
              f"cluster ${risk_budget.max_cluster_premium_usd or 0:,.0f} | "
              f"daily worst case ${risk_budget.daily_loss_limit_usd or 0:,.0f}")

    try:
        asyncio.run(pump_listener(listener, args.limit))
    except GatewayFatalError as exc:
        print(f"gateway fatal: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        if listener.trace is not None and args.trace is not None:
            saved = listener.trace.save(args.trace)
            print(f"trace  | {len(listener.trace.events)} event(s) -> {saved}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """The fill log is the product; verify it can be read back intact.

    A gap means a lost decision, a duplicate means a double-counted fill, and a
    torn last line means a crash mid-append. None may pass silently: a book
    reconstructed from a corrupt log would trade on a lie. Since the intent row
    arrived, there is one more defect: an intent with no later terminal - an
    order that may or may not have reached the venue. That is a divergence, not
    a clean log. A `transport_unknown` row explains one cause of that divergence
    (the socket died mid-order); it is named here so the operator can tell a
    venue ambiguity from a plain crash, and either way the answer is the same:
    resolve it with `recover` from a lookup, never with a retry.
    """
    fills_path = args.fills or FILL_LOG
    if not fills_path.exists():
        print(f"no fill log yet at {fills_path}")
        return 0
    problems = FillLog().verify_file(fills_path)
    rows = FillLog().replay_file(fills_path)
    print(f"fill log: {len(rows)} rows at {fills_path}")
    if problems:
        for problem in problems:
            print(f"  PROBLEM {problem}")
    unresolved = Ledger.unresolved(rows)
    quarantined = Ledger.quarantine_reasons(rows)
    if unresolved:
        print(f"{len(unresolved)} unresolved intent(s) - a decision with no terminal:")
        for intent in unresolved:
            why = quarantined.get(intent.note)
            cause = f" [transport unknown: {why}]" if why else ""
            print(f"  UNRESOLVED {intent.seq} {intent.contract_key:<28} "
                  f"{intent.qty:>3}x key={intent.note} ({intent.reason}){cause}")
    if problems or unresolved:
        if problems:
            print(f"{len(problems)} problem(s): the log cannot be trusted as-is")
        else:
            print("run `recover` to resolve them from a broker lookup")
        return 1
    print("intact: sequence contiguous, every row readable, no unresolved intents")
    return 0


def make_recover_broker(kind: str) -> PaperBroker | RobinhoodAgenticBroker:
    """The lookup-only broker for `recover`; paper is deterministic, Robinhood
    reads MFF_RH_ORDER_LOOKUP_TEMPLATE (see RobinhoodAgenticBroker.lookup)."""
    if kind == "paper":
        return PaperBroker()
    return RobinhoodAgenticBroker()


def cmd_recover(args: argparse.Namespace) -> int:
    """Close the mid-order silence: turn unresolved intents into terminals.

    The ONLY thing recover may do is ask the broker what happened to each
    unresolved idempotency key and append the answer: SEEN -> recovered,
    NOT_SEEN -> not_sent, CANNOT_SAY -> the row stays unresolved and the exit
    code is 1. It never places an order and never retries one - the whole point
    is to stop guessing what a dead process may or may not have sent.
    """
    fills_path = args.fills or FILL_LOG
    if not fills_path.exists():
        print(f"no fill log yet at {fills_path}")
        return 0
    rows = FillLog().replay_file(fills_path)
    unresolved = Ledger.unresolved(rows)
    print(f"recover: {len(rows)} rows, {len(unresolved)} unresolved intent(s) at {fills_path}")
    if not unresolved:
        return 0

    lookup = make_recover_broker(args.broker)
    next_seq = max(f.seq for f in rows) + 1
    appended: list[Fill] = []
    still_unresolved: list[Fill] = []
    for intent in unresolved:
        result = lookup.lookup(intent.note)
        if result is OrderLookup.CANNOT_SAY:
            still_unresolved.append(intent)
            print(f"  UNRESOLVED {intent.contract_key} key={intent.note} (lookup cannot_say)")
            continue
        event = "recovered" if result is OrderLookup.SEEN else "not_sent"
        appended.append(Fill(
            seq=next_seq, ts=time.time(), event=event, contract_key=intent.contract_key,
            qty=intent.qty, price=intent.price, reason=f"{event}:{intent.reason}",
            accepted=True, latency_ms=None, note=intent.note,
        ))
        next_seq += 1
        print(f"  {event:<9} {intent.contract_key} key={intent.note}")

    if appended:
        FillLog(path=fills_path).append_rows(appended)
        print(f"appended {len(appended)} terminal row(s) to {fills_path}")
    if still_unresolved:
        print(f"{len(still_unresolved)} intent(s) still unresolved (lookup cannot_say) - "
              f"re-run `recover` once the lookup can decide")
        return 1
    return 0


def cmd_flatten(args: argparse.Namespace, quotes: QuoteProvider | None = None,
                now: float | None = None) -> int:
    """Get flat on purpose: every confirmed lot, at a bid that actually exists.

    The kill switch stops ENTRIES; it must never trap a position, so this command does
    not consult it. The default is a dry run that touches nothing. `--live` places each
    sell through the live listener - the same intent/ack/book/reconcile path an exit
    post uses - and every sell is at the observed bid, never at the posted price, the
    entry, or a market order. An unpriceable lot is reported and left on the book; a lot
    whose exit is already at the venue is reported and never sold twice.

    Exit 0 when the confirmed book is flat at the end (or a plan covers every lot);
    1 when a lot remains or could not be priced/sold; 2 when the book or quote feed
    cannot be trusted.
    """
    from . import flatten as flatten_mod
    from .execution_book import ExecutionBook, ExecutionBookError

    engine = Engine(fills=FillLog(path=FILL_LOG) if args.live else FillLog())
    try:
        book = ExecutionBook.load(EXECUTION_LOG, day_of=engine.trading_day)
    except ExecutionBookError as exc:
        print(f"refusing to flatten: the confirmed execution log at {EXECUTION_LOG} "
              f"cannot be trusted: {exc}", file=sys.stderr)
        return 2

    if not book.open_positions():
        # A flat book needs no quote feed and no broker. Saying so is the useful answer.
        if args.json:
            print(json.dumps({"mode": "execute" if args.live else "plan",
                              "flat": True, "exit_code": 0, "lines": [],
                              "remaining_positions": 0, "remaining_contracts": 0},
                             indent=2))
        else:
            print("flatten: nothing to flatten - the confirmed book is flat")
        return 0

    if quotes is None:
        quotes = HttpQuoteProvider()
        if not quotes.configured:
            print("refusing to flatten: no quote feed configured "
                  "(set MFF_QUOTE_URL_TEMPLATE). A sale at an invented bid is not a "
                  "flatten.", file=sys.stderr)
            return 2

    if not args.live:
        report = flatten_mod.plan(book, quotes)
        print(json.dumps(report.to_dict(), indent=2) if args.json else report.to_text())
        return report.exit_code

    if not os.environ.get("MFF_RH_TOKEN"):
        print("--live needs MFF_RH_TOKEN", file=sys.stderr)
        return 1
    listener = Listener(
        transport=TapeTransport([]),
        engine=engine,
        broker=RobinhoodAgenticBroker(dry_run=False),
        quotes=quotes,
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=HEARTBEAT),
        kill=KillSwitch(path=NO_TRADE),
        execution=book,
    )
    report = flatten_mod.execute(book, engine, listener, quotes,
                                 time.time() if now is None else now)
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.to_text())
    return report.exit_code


def cmd_marks(args: argparse.Namespace) -> int:
    """The open book, marked to market: what it is worth right now, and whether
    any armed breakeven stop is through. Total is withheld when any position
    cannot be priced - a partial total would be a lie in a summary."""
    from . import stops as stops_mod

    fills_path = args.fills or FILL_LOG
    fills = FillLog().replay_file(fills_path) if fills_path.exists() else []
    book = stops_mod.book_from_fills(fills)

    if args.quotes is not None:
        bids = stops_mod.load_bids(args.quotes)
    else:
        provider = HttpQuoteProvider()
        bids = {}
        for key in book:
            try:
                quote = provider.quote(key)
            except Exception:
                quote = None
            if quote is not None:
                bids[key] = quote.bid

    marks = stops_mod.mark_book(book.values(), bids)
    total = marks.total_unrealized_usd
    # A stop that is through is a decision the engine owes but has not made; a
    # book that cannot be priced is the 9/8 failure in different clothes. Either
    # is a page, so `--check` is the cron tripwire the report's `watch` sibling
    # is not: it pages on the book's P&L, not on the heartbeat.
    at_risk = marks.unpriced > 0 or marks.stopped_count > 0

    if args.json:
        print(json.dumps({
            "positions": len(book),
            "priced": marks.priced,
            "unpriced": marks.unpriced,
            "stopped_count": marks.stopped_count,
            "total_unrealized_usd": total,
            "marks": [
                {"contract_key": m.contract_key, "contracts": m.contracts,
                 "avg_price": m.avg_price, "bid": m.bid,
                 "unrealized_usd": m.unrealized_usd,
                 "breakeven_stop": m.breakeven_stop, "stopped": m.stopped}
                for m in marks.marks
            ],
        }, indent=2))
        return 1 if (args.check and at_risk) else 0

    print(f"book: {len(book)} position(s) | priced {marks.priced} | "
          f"unpriced {marks.unpriced} | stopped {marks.stopped_count}")
    if total is None:
        print("unrealised P&L: WITHHELD (some position cannot be priced)")
    else:
        print(f"unrealised P&L: ${total:,.2f}")
    for m in marks.marks:
        flag = "STOP" if m.stopped else "    "
        print(f"  {flag} {m.contracts:>3}x {m.contract_key:<28} "
              f"avg {m.avg_price:<7.2f} bid {m.bid:<7.2f} "
              f"unreal ${m.unrealized_usd:>9,.2f}")
    return 1 if (args.check and at_risk) else 0


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


# ── expiry audit ─────────────────────────────────────────────────────────────
# Phrases that state a FINAL exit of the whole position. Deliberately narrow: "50%" or
# "runners at 100%" is a scale-out, not a close, so counting those would overstate how
# much of the record actually finished. The result is therefore a LOWER bound on
# "never closed", which is the honest direction for a risk number - and it is checked
# against the corpus's own outcome grading (win_partial + unknown) below.
FINAL_CLOSE_PHRASES: tuple[str, ...] = (
    r"\ball out\b", r"\bfully closed\b", r"\bclosed fully\b", r"\bfull close\b",
    r"\bclosed full position\b", r"\bclosed out\b", r"\bclose fully\b",
    r"\bsold remaining\b", r"\bsold majority\b", r"\bclosed remaining\b",
    r"\bclose remaining\b", r"\blast runner\b", r"\bout at\b", r"\bsl close\b",
    r"\bstop loss\b", r"\bclosed at breakeven\b", r"\bclosed no profit\b",
    r"\bexplicit sl\b", r"\bclosed \d", r"\bclosed at \d", r"\bexit(?:ed)? all\b",
)
_FINAL_CLOSE_RE = re.compile("|".join(FINAL_CLOSE_PHRASES), re.IGNORECASE)
_HHMM_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
UNCLOSED_OUTCOMES = ("win_partial", "unknown")


def _notes_exit_mark(notes: str, entry_ts: float) -> tuple[int, int] | None:
    """The last HH:MM in the notes that is not before the entry - or None.

    None is the interesting answer: it means the record never said when (or whether) the
    position was left, which is precisely the case the cutoff guard exists for.
    """
    from datetime import datetime

    from . import historic

    entry = datetime.fromtimestamp(entry_ts, historic.ET)
    marks = [(int(h), int(m)) for h, m in _HHMM_RE.findall(notes or "")]
    later = [mark for mark in marks if mark >= (entry.hour, entry.minute)]
    return max(later) if later else None


def expiry_audit(corpus: str | Path | None = None,
                 cutoff_minutes: int = 15) -> dict[str, object]:
    """Measure the expiry guard against the only record that exists. Read-only.

    Three questions, and a refusal:

      1. how much of the corpus is 0DTE (a contract that dies at the bell)?
      2. how many alerts never recorded an explicit final close - i.e. how many
         positions the live book would still be carrying?
      3. of the same-day ones, what share would the cutoff force-close, and what share
         has no recorded exit time at all (those PAGE - they cannot be force-closed on
         a price nobody wrote down)?

    And the refusal: **assignment is not projected.** The corpus records the option
    premium (`entry_bid`, `exit_pct`) and nothing about the underlying, so the shares a
    0DTE would deliver are unknowable from it. `100 x contracts` here would be an
    invention, and a missing price is not an out-of-the-money contract.
    """
    from datetime import date

    from . import historic
    from .calendar import CalendarError, session_for
    from .expiry import SAME_DAY_TOKENS

    path = Path(corpus) if corpus is not None else historic.DEFAULT_CORPUS
    trades = historic.load_trades(path)
    if not trades:
        raise ValueError(f"empty corpus at {path}")

    def is_same_day(trade: historic.Trade) -> bool:
        return (trade.expiry.strip() == trade.date.strip()
                or trade.expiry.strip().upper() in SAME_DAY_TOKENS)

    same_day = [t for t in trades if is_same_day(t)]
    unclosed = [t for t in trades if not _FINAL_CLOSE_RE.search(t.notes or "")]
    floor = [t for t in trades if t.outcome in UNCLOSED_OUTCOMES]
    outcome_counts: dict[str, int] = {}
    for trade in same_day:
        outcome_counts[trade.outcome] = outcome_counts.get(trade.outcome, 0) + 1

    forced = 0
    known = 0
    no_mark = 0
    latest: tuple[int, int] | None = None
    cutoff_et_marks: set[str] = set()
    half_days = 0
    no_session = 0
    for trade in same_day:
        try:
            session = session_for(date.fromisoformat(trade.date))
        except (CalendarError, ValueError):
            session = None
        if session is None:
            no_session += 1
            continue
        half_days += int(session.half_day)
        cutoff = session.cutoff_utc(cutoff_minutes).astimezone(historic.ET)
        cutoff_et_marks.add(f"{cutoff.hour:02d}:{cutoff.minute:02d}")
        mark = _notes_exit_mark(trade.notes, trade.entry_ts)
        if mark is None:
            no_mark += 1
            continue
        known += 1
        if latest is None or mark > latest:
            latest = mark
        if mark >= (cutoff.hour, cutoff.minute):
            forced += 1

    def pct(part: int, whole: int) -> float:
        return round(100.0 * part / whole, 2) if whole else 0.0

    def hhmm(mark: tuple[int, int] | None) -> str | None:
        return None if mark is None else f"{mark[0]:02d}:{mark[1]:02d}"

    floor_detail = " + ".join(f"{sum(1 for t in trades if t.outcome == name)} {name}"
                              for name in UNCLOSED_OUTCOMES)
    return {
        "corpus": str(path),
        "alerts": len(trades),
        "days": len({t.date for t in trades}),
        "first_date": min(t.date for t in trades),
        "last_date": max(t.date for t in trades),
        "same_day": {
            "count": len(same_day),
            "of": len(trades),
            "share_pct": pct(len(same_day), len(trades)),
            "by_outcome": dict(sorted(outcome_counts.items(), key=lambda kv: -kv[1])),
            "half_day_sessions": half_days,
            "no_session": no_session,
        },
        "no_explicit_close": {
            "count": len(unclosed),
            "of": len(trades),
            "share_pct": pct(len(unclosed), len(trades)),
            "same_day": sum(1 for t in unclosed if is_same_day(t)),
            "outcome_floor": len(floor),
            "outcome_floor_detail": floor_detail,
            "consistent_with_floor": len(unclosed) >= len(floor),
        },
        "cutoff": {
            "minutes": cutoff_minutes,
            "cutoff_et": sorted(cutoff_et_marks),
            "force_close": forced,
            "known_exit_time": known,
            "no_exit_time": no_mark,
            "share_of_same_day_pct": pct(forced, len(same_day)),
            "share_of_known_pct": pct(forced, known),
            "latest_exit_et": hhmm(latest),
            "clock_caveat": (
                "the corpus carries no timezone proof: its recorded entry times precede "
                "the 09:30 ET open, so the cutoff share is computed on the declared ET "
                "clock and the recorded marks are read as written"),
        },
        "assignment": {
            "status": "refused",
            "shares": None,
            "reason": (
                "the corpus records the option premium only: it has no underlying close "
                "at expiry, so assignment shares cannot be projected. A missing price "
                "is not an out-of-the-money contract"),
            "unresolved_positions": no_mark,
        },
    }


def _expiry_audit_text(audit: dict[str, object]) -> str:
    same = audit["same_day"]
    unclosed = audit["no_explicit_close"]
    cutoff = audit["cutoff"]
    assignment = audit["assignment"]
    assert isinstance(same, dict) and isinstance(unclosed, dict)
    assert isinstance(cutoff, dict) and isinstance(assignment, dict)
    marks = ", ".join(cutoff["cutoff_et"]) or "?"
    latest = cutoff["latest_exit_et"] or "none on record"
    lines = [
        f"expiry audit - {audit['corpus']}",
        f"{audit['alerts']} alerts over {audit['days']} sessions "
        f"({audit['first_date']} .. {audit['last_date']})",
        "",
        f"same-day expiry         {same['count']:>3} / {same['of']}  "
        f"({same['share_pct']:.1f}%)   every one dies at the bell",
        f"no explicit final close {unclosed['count']:>3} / {unclosed['of']}  "
        f"({unclosed['share_pct']:.1f}%)   floor {unclosed['outcome_floor']} from the "
        f"outcomes ({unclosed['outcome_floor_detail']})",
        f"   same-day among them   {unclosed['same_day']:>3} / {same['count']} same-day",
        f"cutoff force-close      {cutoff['force_close']:>3} / {same['count']} same-day "
        f"({cutoff['share_of_same_day_pct']:.1f}% of same-day, "
        f"{cutoff['share_of_known_pct']:.1f}% of the {cutoff['known_exit_time']} with a "
        f"recorded exit time)",
        f"   cutoff {marks} ET for a {cutoff['minutes']}m window, latest recorded "
        f"same-day exit {latest} ET, half-day sessions {same['half_day_sessions']}",
        f"   no recorded exit time {cutoff['no_exit_time']:>3} / {same['count']} - at the "
        f"cutoff these PAGE (HALT_AND_PAGE) instead of flattening on an invented price",
        f"assignment projection   REFUSED - {assignment['reason']}",
        "",
        f"clock caveat: {cutoff['clock_caveat']}",
    ]
    if not unclosed["consistent_with_floor"]:
        lines.append(
            f"WARNING: the notes show only {unclosed['count']} unclosed, below the "
            f"{unclosed['outcome_floor']} the outcome grading implies - the phrase list "
            f"or the corpus has drifted")
    return "\n".join(lines)


def cmd_expiry(args: argparse.Namespace) -> int:
    """`expiry audit`: the guard, measured against the corpus. Places nothing."""
    if args.expiry_cmd != "audit":                     # pragma: no cover - argparse
        print(f"unknown expiry subcommand: {args.expiry_cmd}", file=sys.stderr)
        return 2
    try:
        audit = expiry_audit(args.corpus, cutoff_minutes=int(args.cutoff_minutes))
    except (OSError, ValueError) as exc:
        print(f"cannot audit {args.corpus or 'the default corpus'}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(audit, indent=2))
    else:
        print(_expiry_audit_text(audit))
    unclosed = audit["no_explicit_close"]
    assert isinstance(unclosed, dict)
    return 0 if unclosed["consistent_with_floor"] else 1


def cmd_shadow(args: argparse.Namespace) -> int:
    """`shadow`: the same tape through the decision wiring and the live wiring.

    Exit 0 means every event produced the same intents, the same decision rows with the
    same reasons and keys, and the same book state. Exit 1 names the first difference and
    its likely cause. Exit 2 means there was nothing trustworthy to compare - an empty
    tape, a malformed trace, an invalid venue model - because a parity run over no
    evidence is not a pass, it is a green light with the bulb removed.
    """
    from . import parity as parity_mod

    if args.diff is not None:
        try:
            reference = parity_mod.Trace.load(args.diff[0])
            candidate = parity_mod.Trace.load(args.diff[1])
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return _emit_parity(parity_mod.compare(reference, candidate), args)

    if args.tape is None:
        print("shadow needs a tape, or --diff REFERENCE CANDIDATE", file=sys.stderr)
        return 2
    try:
        tape = load_tape(args.tape)
        quotes = load_quotes(args.tape)
        venue = parity_mod.VenuePolicy(
            fill_fraction=float(args.venue_fill),
            reject=str(args.venue_reject or ""),
            transport=str(args.venue_transport or ""),
        )
    except (OSError, ValueError, TypeError) as exc:
        print(f"cannot shadow {args.tape}: {exc}", file=sys.stderr)
        return 2
    if not tape:
        print(f"{args.tape} has no messages - parity over nothing is not a pass",
              file=sys.stderr)
        return 2

    cfg = EngineConfig(pipe_test=not args.no_pipe_test)
    try:
        report = parity_mod.compare_modes(
            tape, quotes, engine_config=cfg, venue=venue, tape_name=str(args.tape))
    except ValueError as exc:
        print(f"parity could not run: {exc}", file=sys.stderr)
        return 2

    if args.trace_dir is not None:
        # A reviewer can re-run the comparison without the tape, which is the point of a
        # manifest: the artefact travels, the branch does not.
        directory = Path(args.trace_dir)
        directory.mkdir(parents=True, exist_ok=True)
        report.reference.save(directory / "replay.json")
        report.candidate.save(directory / "shadow.json")
    return _emit_parity(report, args)


def _emit_parity(report: ParityReport, args: argparse.Namespace) -> int:
    """Print a parity report and translate its verdict into the process exit code."""
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(report.to_text())
    return 0 if report.ok else 1


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
            "ranking": [{"channel": name, "score": round(score, 4)}
                        for name, score, _ in desk.ranking],
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
    from . import historic
    from . import tca as tca_mod

    if args.tca_cmd == "fills":
        path = args.executions or (ROOT / "live" / "executions.jsonl")
        log = tca_mod.ExecutionLog.load(path)
        fill_report = tca_mod.FillReport(executions=log.records)
        if args.json:
            print(json.dumps({"executions": len(log.records),
                              "mean_cost_bps": round(fill_report.mean_cost_bps, 1),
                              "p90_cost_bps": round(fill_report.p90_cost_bps, 1),
                              "total_cost_usd": fill_report.total_cost_usd,
                              "at_or_better_pct": round(fill_report.at_or_better_share, 1),
                              "partial_pct": round(fill_report.partial_share, 1),
                              "latency_p90_ms": round(fill_report.latency_p90_ms, 1),
                              "suggested_ask_multiple": fill_report.suggested_ask_multiple(),
                              "source": str(path)}, indent=2))
            return 0
        print(fill_report.to_text())
        if not log.records:
            print(f"  (looked in {path})")
        return 0

    bars_dir = args.bars or tca_mod.DEFAULT_BARS_DIR
    barsets = tca_mod.load_barsets(bars_dir)
    if not barsets:
        print(f"no bar series under {bars_dir} - nothing to analyse", file=sys.stderr)
        return 2
    path_report = tca_mod.analyse_paths(barsets, historic.load_trades(),
                                        ask_multiple=args.ask_multiple)
    if args.md is not None:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(path_report.to_markdown(), encoding="utf-8")
    if args.json:
        measurable = path_report.measurable_enough
        print(json.dumps({
            "barsets": path_report.barsets, "joined": path_report.n,
            "measurable": len(path_report.measurable),
            "entry_before_coverage": path_report.n - len(path_report.in_coverage),
            "aggregates_withheld": not measurable,
            "mae_median_pct": round(path_report.mae_median, 2) if measurable else None,
            "mfe_median_pct": round(path_report.mfe_median, 2) if measurable else None,
            "stop30_hit_pct": round(path_report.stop30_share, 1) if measurable else None,
            "carry_median_pct": round(path_report.carry_median, 2),
            "expired_pct": round(path_report.expired_share, 1),
            "unmatched": path_report.unmatched,
            "rows": [r.to_dict() for r in path_report.rows],
        }, indent=2))
        return 0
    print(path_report.to_text())
    if args.md is not None:
        print(f"\nmarkdown report -> {args.md}")
    return 0


def cmd_exposure(args: argparse.Namespace) -> int:
    """The correlated-book cap: write the limits file once, then inspect the book."""
    from . import exposure as exposure_mod

    path = args.path or (ROOT / "live" / "exposure.json")
    if args.exposure_cmd == "init":
        if Path(path).exists():
            print(f"refusing to overwrite {path} - exposure limits are a decision, "
                  "not a generated file")
            return 2
        init_limits = exposure_mod.ExposureLimits()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(init_limits.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"wrote default exposure limits -> {path}")
        print(json.dumps(init_limits.to_dict(), indent=2))
        return 0

    limits: exposure_mod.ExposureLimits | None = None
    if Path(path).exists():
        try:
            limits = exposure_mod.load_limits(path)
        except (OSError, ValueError) as exc:
            print(f"cannot read exposure limits at {path}: {exc}", file=sys.stderr)
            return 2

    fills_path = args.fills or FILL_LOG
    fills = FillLog().replay_file(fills_path) if fills_path.exists() else []
    positions = exposure_mod.positions_from_fills(fills)
    book = exposure_mod.book_exposure(positions)

    if args.json:
        print(json.dumps({
            "limits": limits.to_dict() if limits else {"active": False},
            "book": book,
        }, indent=2))
        return 0

    if limits is None:
        print(f"exposure limits: OFF (no {path})")
    else:
        cap = limits.max_premium_per_underlying_usd
        print(f"exposure limits: per underlying {limits.max_per_underlying} | "
              f"per expiry {limits.max_per_expiry} | "
              f"premium cap {'$' + f'{cap:,.0f}' if cap else 'off'}")
    print(f"book: {book['positions']} position(s) | gross premium "
          f"${book['gross_premium_usd']:,.2f} | net ${book['net_premium_usd']:,.2f}")
    for ticker, row in book["per_underlying"].items():
        print(f"  {ticker:<8} {row['positions']} pos {row['contracts']} contracts "
              f"premium ${row['premium_usd']:,.2f} (calls {row['calls']} / puts {row['puts']})")
    for expiry, row in book["per_expiry"].items():
        print(f"  exp {expiry:<12} {row['positions']} pos {row['contracts']} contracts "
              f"premium ${row['premium_usd']:,.2f}")
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


def cmd_risk(args: argparse.Namespace) -> int:
    """The pre-trade budget: price the loss before the order, then show the headroom."""
    from . import exposure as exposure_mod
    from .risk import RiskBudget

    path = args.path or (ROOT / "live" / "risk-budget.json")
    if args.risk_cmd == "init":
        if Path(path).exists():
            print(f"refusing to overwrite {path} - a risk budget is a decision, "
                  "not a generated file")
            return 2
        starter = RiskBudget.load(ROOT / "live" / "risk-budget.example.json",
                                  missing_ok=False)
        if starter is None:                     # pragma: no cover - missing_ok=False raises
            return 2
        starter.save(path)
        print(f"wrote reviewed starter budget -> {path}")
        print(json.dumps(starter.to_dict(), indent=2))
        return 0

    try:
        budget = RiskBudget.load(path)
    except (OSError, ValueError) as exc:
        print(f"cannot read risk budget at {path}: {exc}", file=sys.stderr)
        return 2

    if args.risk_cmd == "validate":
        if budget is None:
            print(f"risk budget: OFF (no {path})")
            return 0
        if args.json:
            print(json.dumps(budget.to_dict(), indent=2))
        else:
            print(f"risk budget: valid | total "
                  f"${budget.max_total_premium_usd or 0:,.0f} | underlying "
                  f"${budget.max_underlying_premium_usd or 0:,.0f} | cluster "
                  f"${budget.max_cluster_premium_usd or 0:,.0f} | daily worst case "
                  f"${budget.daily_loss_limit_usd or 0:,.0f}")
        return 0

    if budget is None:
        print(f"risk budget: OFF (no {path})")
        return 0
    fills_path = args.fills or FILL_LOG
    fills = FillLog().replay_file(fills_path) if fills_path.exists() else []
    positions = exposure_mod.positions_from_fills(fills)
    realized = args.realized_today
    if realized is None:
        from .governor import GovernorState
        try:
            realized = GovernorState.load(ROOT / "live" / "governor.json").realized_today_usd
        except ValueError:
            realized = 0.0
    report = budget.report(positions, realized_today_usd=realized)
    if args.json:
        print(json.dumps({"budget": budget.to_dict(), "usage": report}, indent=2))
        return 0
    print(f"risk budget: {report['open_positions']} open | premium at risk "
          f"${report['premium_at_risk_usd']:,.2f} | realised today "
          f"${report['realized_today_usd']:,.2f} | worst-case day "
          f"${report['worst_case_day_usd']:,.2f}")
    print(f"  headroom : total {_money(report['total_headroom_usd'])} | "
          f"daily {_money(report['daily_headroom_usd'])}")
    for label, key in (("underlying", "by_underlying"), ("cluster", "by_cluster")):
        rows = report[key]
        if rows:
            print(f"  by {label}:")
            for name, premium in rows.items():
                print(f"    {name:<44} ${premium:>10,.2f}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """The book we hold vs the book the venue holds. Drift is a page, not a guess.

    The **source** of the local book matters as much as the comparison. Two exist:

    * `confirmed` - the execution book (`live/execution.jsonl`), the venue's own answers;
    * `fills` - the optimistic reconstruction from the fill log, which books a buy at
      decision time and drops a position at sell-INTENT time, so it can believe it is
      flat while the venue still holds the contracts.

    `auto` (the default) uses the confirmed book when it has recorded at least one order
    and falls back to the fill log otherwise, because a fresh install has an empty
    execution log and comparing an empty book against a real one is a false drift.
    The source used is printed, so a reviewer never has to infer it.

    Exit codes are the contract: 0 clean, 1 drift, 2 malformed/stale/unreadable. A
    snapshot with no timestamp is refused when `--max-age` is set: an undatable book is
    not evidence, and a stale one is worse than none.
    """
    from . import exposure as exposure_mod
    from .execution_book import ExecutionBook, ExecutionBookError
    from .reconcile import ReconcileError, reconcile
    from .venue import FileVenueFeed, VenueError

    feed = FileVenueFeed(path=args.snapshot, max_age_s=args.max_age)
    try:
        snapshot = feed.snapshot()
    except (VenueError, ReconcileError) as exc:
        print(f"cannot use venue snapshot: {exc}", file=sys.stderr)
        return 2

    confirmed: ExecutionBook | None = None
    if EXECUTION_LOG.exists():
        try:
            confirmed = ExecutionBook.load(EXECUTION_LOG)
        except ExecutionBookError as exc:
            print(f"cannot read the confirmed book at {EXECUTION_LOG}: {exc}",
                  file=sys.stderr)
            return 2

    if args.source == "confirmed" and (confirmed is None or not confirmed.orders):
        print(f"no confirmed execution book at {EXECUTION_LOG} "
              f"(use --source fills to compare the optimistic book)", file=sys.stderr)
        return 2
    if args.source == "fills":
        source = "fills"
    elif args.source == "confirmed":
        source = "confirmed"
    else:
        source = "confirmed" if confirmed is not None and confirmed.orders else "fills"

    if source == "confirmed":
        assert confirmed is not None
        local = confirmed.open_positions()
    else:
        fills_path = args.fills or FILL_LOG
        fills = FillLog().replay_file(fills_path) if fills_path.exists() else []
        local = exposure_mod.positions_from_fills(fills)

    try:
        report = reconcile(local, snapshot.positions)
    except ReconcileError as exc:
        print(f"cannot reconcile: {exc}", file=sys.stderr)
        return 2

    age = snapshot.age_s()
    if args.json:
        print(json.dumps({"snapshot": str(args.snapshot),
                          "venue": snapshot.to_dict(),
                          "local_source": source,
                          "report": report.to_dict()}, indent=2))
    else:
        taken = "unknown" if snapshot.taken_at is None else f"{age:.0f}s ago"
        print(f"reconcile | local {report.local_positions} position(s) / "
              f"{report.local_contracts} contract(s) [{source}] | venue "
              f"{report.venue_positions} / {report.venue_contracts} "
              f"({snapshot.source}, {taken})")
        if report.clean:
            print("  CLEAN - the two books agree")
        else:
            print(f"  DRIFT - {len(report.drifts)} disagreement(s):")
            for drift in report.drifts:
                print(f"    {drift.kind:<18} {drift.contract_key:<28} "
                      f"local={drift.local:<4} venue={drift.venue}")
            print("  next: halt new buys now (touch live/NO_TRADE), resolve the "
                  "disagreement, then re-arm")
    return 0 if report.clean else 1


def cmd_book(args: argparse.Namespace) -> int:
    """The confirmed book: what the venue answered, rebuilt from an append-only log.

    The fill log records *decisions*; this book records *confirmed executions*. It is
    the difference between "we decided to sell" and "the venue says we sold 3 of 5".
    Every subcommand is offline: nothing here places, retries or cancels.

    Exit codes follow the rest of the operator surface: 0 clean, 1 a defect (or, with
    `--strict`, an unresolved order), 2 a log that cannot be used as evidence.
    """
    from .execution_book import ExecutionBook, ExecutionBookError, utc_day

    path = args.path or EXECUTION_LOG
    engine = Engine() if args.book_cmd == "sync" else None
    # The day key must match the engine's ET trading day, or a 20:00 ET settle is
    # filed under tomorrow. `load` only groups events; it never invents one.
    day_of = engine.trading_day if engine is not None else utc_day
    try:
        book = ExecutionBook.load(path, day_of=day_of)
    except ExecutionBookError as exc:
        print(f"cannot use the execution log at {path}: {exc}", file=sys.stderr)
        return 2

    if args.book_cmd == "show":
        if args.json:
            print(json.dumps({"path": str(path), "digest": book.digest(),
                              "book": book.snapshot(),
                              "unresolved": [o.to_dict() for o in book.unresolved_orders()]},
                             indent=2))
            return 0
        print(book.report_text())
        print(f"  log      {path}")
        return 0

    if args.book_cmd == "replay":
        try:
            again = ExecutionBook.load(path, day_of=day_of)
        except ExecutionBookError as exc:          # pragma: no cover - first load checked
            print(f"cannot replay the execution log: {exc}", file=sys.stderr)
            return 2
        same = book.digest() == again.digest()
        if args.json:
            print(json.dumps({"path": str(path), "identical": same,
                              "digest": book.digest(), "replay_digest": again.digest(),
                              "events_replayed": len(book.orders) + len(book.fills),
                              "torn_lines": book.torn_lines}, indent=2))
            return 0 if same else 1
        print(f"replay | {path}")
        print(f"  digest        {book.digest()}")
        print(f"  replayed      {again.digest()}")
        print(f"  orders        {len(book.orders)} | fills {len(book.fills)} | "
              f"trades {len(book.realized)}")
        if book.torn_lines:
            print(f"  torn lines    {book.torn_lines} (final append only, ignored)")
        print("  IDENTICAL - the same log rebuilds the same book" if same else
              "  DIVERGED - replay is not deterministic; do not trade on this log")
        return 0 if same else 1

    if args.book_cmd == "verify":
        problems = book.problems()
        unresolved = book.unresolved_orders()
        unknown = book.unknown_orders()
        failed = bool(problems) or (bool(args.strict) and bool(unresolved))
        if args.json:
            print(json.dumps({"path": str(path), "ok": not failed,
                              "problems": problems,
                              "unresolved": [o.to_dict() for o in unresolved],
                              "unknown": [o.client_id for o in unknown],
                              "anomalies": book.snapshot()["anomalies"],
                              "duplicates": book.duplicates,
                              "torn_lines": book.torn_lines}, indent=2))
            return 1 if failed else 0
        print(f"execution log: {len(book.orders)} order(s) at {path}")
        for problem in problems:
            print(f"  PROBLEM {problem}")
        if unresolved:
            print(f"{len(unresolved)} unresolved order(s) - sent, no venue answer:")
            for order in unresolved:
                cause = f" [transport unknown: {order.last_unknown}]" if order.unknown else ""
                print(f"  UNRESOLVED {order.state:<8} {order.action:<4} "
                      f"{order.contract_key:<28} {order.filled_qty}/{order.intent.qty} "
                      f"key={order.client_id}{cause}")
        if book.anomalies:
            print(f"  anomalies {dict(sorted(book.anomalies.items()))}")
        if book.duplicates:
            print(f"  duplicate answers ignored: {book.duplicates}")
        if book.torn_lines:
            print(f"  one torn final append ignored: {book.torn_lines}")
        if failed:
            if problems:
                print(f"{len(problems)} problem(s): the book cannot be trusted as-is")
            else:
                print("unresolved orders: settle them with `recover` before arming")
            return 1
        if unresolved:
            print("structurally intact; unresolved orders are in flight or awaiting "
                  "`recover` (use --strict to fail on them)")
        else:
            print("intact: every order has a terminal, lots are consistent, "
                  "realised total equals its trades")
        return 0

    if args.book_cmd == "sync":
        assert engine is not None                     # constructed above for day_of
        report = book.sync_engine(engine, args.now if args.now is not None else time.time())
        if args.json:
            print(json.dumps({"path": str(path), "digest": book.digest(),
                              "restored": report.to_dict(),
                              "positions": [p.contract_key for p in engine.open_positions],
                              "realized_usd": engine.stats.realized_usd,
                              "governor": engine.governor_state.to_dict()}, indent=2))
            return 0
        print(f"sync | restored {report.positions} position(s) / {report.contracts} "
              f"contract(s) onto day {report.day}")
        print(f"  realised      ${report.realized_usd:,.2f} | entries today "
              f"{report.entries_today} | settles today {report.trades_today}")
        if report.expired:
            print(f"  expired       {report.expired} carried lot(s) released before the "
                  f"restore (a dead contract must not hold a slot)")
        if report.unresolved:
            print(f"  unresolved    {report.unresolved} order(s) with no venue answer - "
                  f"run `recover` before arming")
        for position in engine.open_positions:
            print(f"  POSITION  {position.contract_key:<28} {position.contracts:>3} @ "
                  f"{position.avg_price:g}  {position.channel}")
        print(f"  digest        {book.digest()[:16]}")
        return 0

    # export: the confirmed fills, in the shape `tca fills` measures
    from . import tca as tca_mod

    rows = book.execution_rows()
    target = args.executions or EXECUTIONS_LOG
    if not rows:
        print("no confirmed fills yet - nothing to export (this is not an error)")
        return 0
    if target.exists() and target.stat().st_size and not args.replace:
        print(f"refusing to overwrite {target}: it already holds executions. "
              f"Re-run with --replace if this is the same book.", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    if args.replace:
        target.write_text("", encoding="utf-8")
    log = tca_mod.ExecutionLog(path=target)
    for row in rows:
        log.record(row)
    partials = sum(1 for row in rows if row.fill_ratio < 1.0)
    if args.json:
        print(json.dumps({"source": str(path), "target": str(target), "executions": len(rows),
                          "partials": partials}, indent=2))
        return 0
    print(f"exported {len(rows)} confirmed fill(s) -> {target} ({partials} partial)")
    print("  next: `tca fills` measures them; it still refuses to suggest an "
          "ask_multiple under ten priced fills")
    return 0


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers = {
        "doctor": cmd_doctor,
        "replay": cmd_replay,
        "arm": cmd_arm,
        "status": cmd_status,
        "verify": cmd_verify,
        "recover": cmd_recover,
        "flatten": cmd_flatten,
        "marks": cmd_marks,
        "backtest": cmd_backtest,
        "watch": cmd_watch,
        "policy": cmd_policy,
        "desk": cmd_desk,
        "tca": cmd_tca,
        "fuzz": cmd_fuzz,
        "exposure": cmd_exposure,
        "risk": cmd_risk,
        "reconcile": cmd_reconcile,
        "book": cmd_book,
        "expiry": cmd_expiry,
        "shadow": cmd_shadow,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

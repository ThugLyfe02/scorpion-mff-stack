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
from .engine import Engine, EngineConfig
from .fills import FillLog
from .health import Heartbeat, LatencyBudget
from .killswitch import KillSwitch
from .listener import Listener, WebsocketGateway
from .parser import CHANNEL_IDS, EXCLUDED_CHANNEL_IDS, EXCLUDED_TICKERS
from .replay import load_quotes, load_tape, replay

ROOT = Path(__file__).resolve().parent.parent
FILL_LOG = ROOT / "live" / "fills.jsonl"
HEARTBEAT = ROOT / "live" / "heartbeat.json"
NO_TRADE = ROOT / "live" / "NO_TRADE"

__version__ = "0.1.0"


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
        engine=Engine(config=EngineConfig(), fills=FillLog(path=FILL_LOG, fsync=False)),
        broker=broker,
        quotes=quotes,
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=HEARTBEAT),
        kill=kill,
    )
    print(f"armed | broker={'LIVE' if live else 'DRY'} | "
          f"quotes={'configured' if quotes.configured else 'NOT configured (entries will reject)'} | "
          f"channels={','.join(CHANNEL_IDS)}")

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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers = {
        "doctor": cmd_doctor,
        "replay": cmd_replay,
        "arm": cmd_arm,
        "status": cmd_status,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

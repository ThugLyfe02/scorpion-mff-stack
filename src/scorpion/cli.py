from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .broker import Quote
from .config import Policy
from .domain import EventKind, SignalEvent
from .outbox import Dispatcher
from .paper import PaperSender
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


def cmd_health(args: argparse.Namespace) -> int:
    print(json.dumps(Store(args.db).health_snapshot(), indent=2, sort_keys=True))
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

    health = sub.add_parser("health", help="heartbeats, halt flag, pending effects")
    health.add_argument("--db", default="scorpion.db")
    health.set_defaults(func=cmd_health)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

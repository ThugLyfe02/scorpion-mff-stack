"""``scorpion`` operator commands for the paper execution core.

scorpion run-session            one supervised asyncio paper session (systemd ExecStart)
scorpion preflight              gate run by ExecStartPre; non-zero keeps the unit down
scorpion replay --date D        deterministic fwd-v2.1 replay of a calls+bars fixture
scorpion latch status|clear     inspect / clear (person only) the NO_TRADE latch
scorpion pull --estimate ...    Databento cost estimate; a pull needs cost <= $30 or approval
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from .broker import ExecutionMode
from .config import (
    DATABENTO_COST_CAP_USD,
    EXECUTION_COMPONENT,
    FORWARD_RULES_SHA256,
    MARKET_TZ,
    ForwardRules,
    load_forward_rules,
)
from .execution_model import DayBars, MinuteBar
from .fail_safe_control import NoTradeSafetyLatch, SafetyMode
from .fill_logger import build_rows, write_csv

REPO_ROOT = Path(__file__).resolve().parents[2]
REPLAY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "replay_fixture.json"
REPLAY_GOLDEN = REPO_ROOT / "tests" / "fixtures" / "replay_golden.csv"
LOGGER = logging.getLogger("scorpion.exec")


def state_dir() -> Path:
    return Path(os.environ.get("SCORPION_STATE_DIR", REPO_ROOT / "var"))


def latch_path() -> Path:
    return state_dir() / "safety_latch.db"


def open_latch() -> NoTradeSafetyLatch:
    path = latch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return NoTradeSafetyLatch(path)


# ------------------------------------------------------------------------------------- replay


def load_fixture(
    path: str | Path, rules: ForwardRules
) -> tuple[list[dict[str, object]], dict[str, dict[str, DayBars]]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    calls = list(payload["calls"])
    store: dict[str, dict[str, DayBars]] = {}
    for symbol, raw_bars in payload["bars"].items():
        days: dict[str, list[MinuteBar]] = {}
        for ts, o, h, low, c in raw_bars:
            stamp = datetime.fromisoformat(ts)
            days.setdefault(stamp.astimezone(MARKET_TZ).strftime("%Y-%m-%d"), []).append(
                MinuteBar(stamp, float(o), float(h), float(low), float(c))
            )
        store[symbol] = {
            day: DayBars(
                bars, session_open=rules.session_open_et, session_last=rules.session_last_bar_et
            )
            for day, bars in days.items()
        }
    return calls, store


def replay_csv(
    fixture: str | Path, *, only_date: str | None = None, rules: ForwardRules | None = None
) -> str:
    rules = rules or load_forward_rules()
    calls, store = load_fixture(fixture, rules)
    return write_csv(build_rows(calls, store, rules, only_date=only_date))


# ---------------------------------------------------------------------------------- preflight


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _databento_auth_check(timeout_s: float = 10.0) -> CheckResult:
    from .databento_feed import historical_client, redact

    async def probe() -> object:
        client = historical_client()
        return await asyncio.wait_for(asyncio.to_thread(client.metadata.list_datasets), timeout_s)

    try:
        datasets = asyncio.run(probe())
    except Exception as exc:
        return CheckResult("databento_auth", False, redact(f"{type(exc).__name__}: {exc}")[:200])
    count = len(datasets) if isinstance(datasets, list) else 0
    return CheckResult("databento_auth", count > 0, f"{count} datasets visible")


def preflight_checks(
    *,
    today: date | None = None,
    feed_check: Callable[[], CheckResult] = _databento_auth_check,
    min_free_bytes: int = 1 << 30,
    environ: Mapping[str, str] = os.environ,
) -> tuple[list[CheckResult], bool]:
    """Returns (results, market_open). Any failed result means: do not start, page."""

    from .session_calendar import CalendarNotCovered, session_hours

    results: list[CheckResult] = []
    day = today or datetime.now(MARKET_TZ).date()
    try:
        hours = session_hours(day)
    except CalendarNotCovered as exc:
        return [CheckResult("xnys_calendar", False, str(exc))], False
    if hours is None:
        return [CheckResult("xnys_session", True, f"{day} is not an XNYS session")], False
    results.append(CheckResult("xnys_session", True, f"{hours[0].time()}-{hours[1].time()} ET"))

    state = open_latch().state(EXECUTION_COMPONENT)
    results.append(
        CheckResult("latch", state.mode is SafetyMode.NORMAL, f"{state.mode.value}: {state.reason}")
    )

    mode = environ.get("SCORPION_BROKER_MODE", "PAPER")
    results.append(CheckResult("broker_mode", mode in {m.value for m in ExecutionMode}, mode))

    target = state_dir()
    target.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target).free
    results.append(CheckResult("disk_free", free >= min_free_bytes, f"{free / 2**30:.1f} GiB free"))

    try:
        rules = load_forward_rules()
        results.append(CheckResult("rules_lock", rules.sha256 == FORWARD_RULES_SHA256, rules.sha256))
    except Exception as exc:
        results.append(CheckResult("rules_lock", False, f"{type(exc).__name__}: {exc}"))
        rules = None
    if rules is not None and REPLAY_FIXTURE.exists() and REPLAY_GOLDEN.exists():
        produced = replay_csv(REPLAY_FIXTURE, rules=rules).encode("utf-8")
        results.append(
            CheckResult(
                "golden_replay", produced == REPLAY_GOLDEN.read_bytes(), f"{len(produced)} bytes"
            )
        )
    else:
        results.append(CheckResult("golden_replay", False, "fixture or golden missing"))
    results.append(feed_check())
    return results, True


# --------------------------------------------------------------------------------- run-session


async def _run_session(args: argparse.Namespace) -> int:
    from .alerts import Pager
    from .databento_feed import DatabentoFeed, DatabentoLiveClient, SecretRedactingFilter
    from .domain import RawDiscordMessage
    from .fastpath import QuoteCache
    from .health import FeedHealthMonitor
    from .session_calendar import session_hours
    from .session_service import (
        Journal,
        JournalError,
        LatchMirror,
        PaperExecutionSession,
        stop_time_utc,
        supervise,
    )

    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretRedactingFilter())
    rules = load_forward_rules()
    if rules.sha256 != FORWARD_RULES_SHA256:
        LOGGER.error("rules lock hash mismatch; refusing to run")
        return 2
    today = datetime.now(MARKET_TZ).date()
    hours = session_hours(today)
    if hours is None:
        LOGGER.info("%s is not an XNYS session; exiting cleanly", today)
        return 0
    cache = QuoteCache()
    latch = LatchMirror(open_latch())
    pager = Pager()
    authors = frozenset(
        a.strip() for a in os.environ.get("SCORPION_SIGNAL_AUTHOR_IDS", "").split(",") if a.strip()
    )
    token = os.environ.get("SCORPION_DISCORD_TOKEN", "").strip()
    if not args.no_discord and (not token or not authors):
        LOGGER.error("paper session requires Discord token and explicit author allowlist")
        return 2
    try:
        journal = Journal(state_dir() / "journal" / f"{today.isoformat()}.jsonl")
    except (OSError, JournalError) as exc:
        latch.trip(f"journal_start:{type(exc).__name__}")
        LOGGER.error("paper journal unavailable; no session started")
        return 2
    session = PaperExecutionSession(
        rules=rules,
        cache=cache,
        latch=latch,
        pager=pager,
        journal=journal,
        allowed_author_ids=authors or None,
    )
    monitor = FeedHealthMonitor(cache, session.stale, started_mono_ns=time.monotonic_ns())
    feed = DatabentoFeed(DatabentoLiveClient(), cache, ("QQQ", "SPY"), on_skew=monitor.observe_skew)
    queue: asyncio.Queue[tuple[RawDiscordMessage, int]] = asyncio.Queue(maxsize=512)
    overflow = asyncio.Event()
    open_et, close_et = hours

    def session_open() -> bool:
        now = datetime.now(MARKET_TZ)
        return now.replace(hour=9, minute=10, second=0, microsecond=0) <= now < close_et

    async def sink(raw: RawDiscordMessage) -> None:
        try:
            queue.put_nowait((raw, time.perf_counter_ns()))
        except asyncio.QueueFull:
            # Discord callbacks can swallow raised exceptions. Signal the supervised
            # task explicitly and immediately close the paper admission latch.
            overflow.set()
            latch.trip("ingress_queue_overflow")

    ingest_run: Callable[[], object] | None = None
    if not args.no_discord:
        from .ingest.discord import DiscordSignalClient

        client = DiscordSignalClient(sink)

        async def discord_ingress() -> None:
            async def start_client() -> None:
                await client.start(token)
                raise RuntimeError("discord_client_ended")

            async def watch_overflow() -> None:
                await overflow.wait()
                raise RuntimeError("ingress_queue_overflow")

            async with asyncio.TaskGroup() as group:
                group.create_task(start_client())
                group.create_task(watch_overflow())

        ingest_run = discord_ingress
    try:
        await supervise(
            session,
            monitor=monitor,
            feed_run=feed.run,
            ingest_queue=queue,
            session_open=session_open,
            stop_at_utc=stop_time_utc(close_et),
            on_new_symbol=feed.add_symbol,
            ingest_run=ingest_run,
        )
    finally:
        try:
            journal.close()
        except JournalError:
            latch.trip("journal_close_failed")
            raise
        (state_dir() / "latency").mkdir(parents=True, exist_ok=True)
        (state_dir() / "latency" / f"{today.isoformat()}.json").write_text(
            json.dumps(session.latency.summary_ms(), indent=2)
        )
    return 0


# -------------------------------------------------------------------------------------- main


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scorpion")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-session")
    run.add_argument("--no-discord", action="store_true", help="feed/health only (drills)")
    sub.add_parser("preflight")
    journal_verify = sub.add_parser("verify-paper-journal")
    journal_verify.add_argument("--journal", type=Path, required=True)
    recovery = sub.add_parser("recover-paper-journal")
    recovery.add_argument("--journal", type=Path, required=True)
    recovery.add_argument("--out", type=Path, required=True)
    feature_report = sub.add_parser("paper-intent-features")
    feature_report.add_argument("--journal", type=Path, required=True)
    alternative_report = sub.add_parser("paper-intent-alternatives")
    alternative_report.add_argument("--journal", type=Path, required=True)
    rp = sub.add_parser("replay")
    rp.add_argument("--date")
    rp.add_argument("--input", type=Path, default=REPLAY_FIXTURE)
    rp.add_argument("--out", type=Path)
    lt = sub.add_parser("latch")
    lt_sub = lt.add_subparsers(dest="latch_command", required=True)
    lt_sub.add_parser("status")
    clear = lt_sub.add_parser("clear")
    clear.add_argument("--operator", required=True)
    clear.add_argument("--reason", required=True)
    trip = lt_sub.add_parser("trip")
    trip.add_argument("--reason", required=True)
    pull = sub.add_parser("pull")
    pull.add_argument("--dataset", required=True)
    pull.add_argument("--schema", required=True)
    pull.add_argument("--symbols", required=True, help="comma separated")
    pull.add_argument("--start", required=True)
    pull.add_argument("--end", required=True)
    pull.add_argument("--estimate", action="store_true")
    pull.add_argument("--approve-cost", action="store_true")
    pull.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    if args.command in ("paper-intent-features", "paper-intent-alternatives"):
        from dataclasses import asdict

        from .causal_features import load_paper_intent_features
        from .counterfactual import compare_paper_intent_alternatives
        from .session_service import JournalError

        try:
            if args.command == "paper-intent-features":
                _print(asdict(load_paper_intent_features(args.journal)))
            else:
                _print(asdict(compare_paper_intent_alternatives(args.journal)))
        except (OSError, JournalError, ValueError) as exc:
            _print({"ok": False, "reason": type(exc).__name__})
            return 1
        return 0
    if args.command == "recover-paper-journal":
        from .recovery import recover_paper_session_journal
        from .session_service import JournalError

        try:
            report = recover_paper_session_journal(args.journal, args.out)
        except (OSError, JournalError, ValueError) as exc:
            _print({"ok": False, "reason": type(exc).__name__})
            return 1
        _print(report)
        return 0
    if args.command == "verify-paper-journal":
        from .session_service import JournalError, verify_paper_journal

        try:
            receipt = verify_paper_journal(args.journal)
        except JournalError as exc:
            _print({"ok": False, "reason": str(exc)})
            return 1
        _print({"ok": True, "receipt": receipt, "certifies_fills": False})
        return 0
    if args.command == "replay":
        text = replay_csv(args.input, only_date=args.date)
        if args.out:
            args.out.write_text(text, encoding="utf-8", newline="")
        else:
            sys.stdout.write(text)
        return 0
    if args.command == "latch":
        latch = open_latch()
        if args.latch_command == "clear":
            state = latch.clear_no_trade(
                EXECUTION_COMPONENT, operator=args.operator, reason=args.reason
            )
        elif args.latch_command == "trip":
            state = latch.trip_no_trade(EXECUTION_COMPONENT, reason=args.reason, actor="operator-cli")
        else:
            state = latch.state(EXECUTION_COMPONENT)
        _print(
            {
                "component": EXECUTION_COMPONENT,
                "mode": state.mode.value,
                "reason": state.reason,
                "updated_by": state.updated_by,
                "updated_ts_utc": state.updated_ts_utc,
            }
        )
        return 0
    if args.command == "preflight":
        results, market_open = preflight_checks()
        _print([{"check": r.name, "ok": r.ok, "detail": r.detail} for r in results])
        if not market_open:
            return 0 if all(r.ok for r in results) else 1
        return 0 if all(r.ok for r in results) else 1
    if args.command == "pull":
        from .databento_feed import CostGuardRefused, PullRequest, guarded_pull, historical_client

        request = PullRequest(
            args.dataset, args.schema, tuple(args.symbols.split(",")), args.start, args.end
        )
        sink = None
        if args.out is not None:
            out_path: Path = args.out

            def sink(data: object) -> None:
                data.to_file(str(out_path))  # type: ignore[attr-defined]

        try:
            decision = guarded_pull(
                historical_client(),
                request,
                approve_cost=args.approve_cost,
                estimate_only=args.estimate,
                sink=sink,
            )
        except CostGuardRefused as exc:
            print(str(exc), file=sys.stderr)
            return 3
        _print(
            {
                "estimated_cost_usd": str(decision.estimated_cost_usd),
                "cap_usd": str(DATABENTO_COST_CAP_USD),
                "approved": decision.approved,
                "executed": decision.executed,
            }
        )
        return 0
    if args.command == "run-session":
        return asyncio.run(_run_session(args))
    return 2


def decimal_or_none(value: str | None) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def _utc_now() -> datetime:
    return datetime.now(UTC)

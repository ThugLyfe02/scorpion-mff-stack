"""The long-running dispatcher: cadence, gating, reconciliation and the metrics series.

Four properties, each about a way a service quietly lies:

1. a healthy tick dispatches a due effect and settles the fill, exactly like the one-shot;
2. a halted desk refuses to dispatch, and an UNKNOWN send blocks new orders until it is
   resolved - the book's fate must be fully known before anything new is sent;
3. reconciliation runs on every tick an UNKNOWN exists, and it never blind-retries;
4. the metrics series is bounded and summarisable, so "did the dispatcher stop" is answered
   by a file rather than a guess.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from scorpion.config import Policy
from scorpion.outbox import Dispatcher
from scorpion.paper import PaperSender, quote
from scorpion.pipeline import Pipeline
from scorpion.reconciliation import LookupResult
from scorpion.scenario import message
from scorpion.service import (
    SeriesLog,
    Service,
    ServiceConfig,
    read_series,
    summarize_series,
)
from scorpion.store import Store

START = datetime.now(UTC).replace(microsecond=0)
KEY = f"QQQ|CALL|719|{START.date().isoformat()}"


def soon() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=1)


def store(tmp_path) -> Store:
    return Store(tmp_path / "s.db")


def quotes(at: datetime) -> dict:
    return {KEY: quote("1.00", "1.02", at=at)}


def pipeline_with_entry(s: Store) -> Pipeline:
    p = Pipeline(store=s)
    asyncio.run(p.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    return p


class Boom:
    def send(self, envelope, *, now):  # noqa: ANN001
        raise TimeoutError("venue did not answer")


class Lookup:
    """A scripted venue lookup: every key answers with one fixed tri-state verdict."""

    def __init__(self, status: str, broker_ref: str = "") -> None:
        self.status = status
        self.broker_ref = broker_ref
        self.keys: list[str] = []

    def find(self, idempotency_key: str) -> LookupResult:
        self.keys.append(idempotency_key)
        return LookupResult(self.status, self.broker_ref)


def service(s: Store, *, lookup=None, config=None, series=None, quotes=None) -> Service:
    return Service(
        s,
        sender=PaperSender(),
        lookup=lookup,
        policy=Policy(live_submission_enabled=True),
        quotes=quotes,
        config=config or ServiceConfig(series_path=series),
    )


# ── a healthy tick ──────────────────────────────────────────────────────────
def test_a_healthy_tick_dispatches_a_due_effect_and_settles(tmp_path):
    s = store(tmp_path)
    pipeline_with_entry(s)
    moment = soon()
    svc = service(s, quotes=quotes(moment))
    report = svc.step(now=moment)
    assert report.dispatched.sent == 1
    assert report.halted is False
    assert report.unknown_after == 0
    assert report.exit_code == 0
    # The fill moved the book: the applied log now carries a fill for the entry.
    kinds = {row["kind"] for row in s.load_applied()}
    assert "fill" in kinds


# ── the two refusals that matter ────────────────────────────────────────────
def test_a_halted_store_refuses_to_dispatch(tmp_path):
    s = store(tmp_path)
    p = pipeline_with_entry(s)
    p.halt("operator stop")
    report = service(s).step(now=soon())
    assert report.halted is True
    assert report.dispatched.attempted == 0
    assert report.exit_code == 1
    assert "halted" in report.problems[0]
    # The due effect is still waiting, not sent, not lost.
    counts = s.outbox_snapshot()["counts"]
    assert counts.get("PENDING", 0) == 1


def test_an_unknown_send_blocks_new_orders_until_resolved(tmp_path):
    s = store(tmp_path)
    p = pipeline_with_entry(s)
    # First entry's send is ambiguous: the transport died, fate unknown.
    moment = soon()
    Dispatcher(
        store=s, sender=Boom(), policy=Policy(live_submission_enabled=True)
    ).dispatch_once(state=p.state, now=moment, quotes=quotes(moment))
    assert s.outbox_snapshot()["counts"] == {"UNKNOWN": 1}

    # A second, independent entry is due. It must NOT be sent while the book's fate is
    # unknowable, because a fill that did happen would make this an oversize.
    asyncio.run(p.handle(message("QQQ 719C TODAY @ 1.01", message_id="m2", at=START)))
    report = service(s).step(now=soon())
    assert report.unknown_after == 1
    assert report.dispatched.attempted == 0
    assert report.exit_code == 1
    assert s.outbox_snapshot()["counts"].get("PENDING", 0) == 1


# ── reconciliation, never a blind retry ─────────────────────────────────────
def test_an_unknown_confirmed_not_seen_is_retried_then_sent(tmp_path):
    s = store(tmp_path)
    p = pipeline_with_entry(s)
    moment = soon()
    Dispatcher(
        store=s, sender=Boom(), policy=Policy(live_submission_enabled=True)
    ).dispatch_once(state=p.state, now=moment, quotes=quotes(moment))

    lookup = Lookup("not_seen")
    report = service(s, lookup=lookup, quotes=quotes(moment)).step(now=moment)
    assert lookup.keys, "the idempotency key was read back and asked"
    assert report.reconciliation is not None
    assert report.reconciliation.retried == 1
    assert report.unknown_after == 0
    # The same tick dispatched the now-retryable effect through the paper venue.
    assert report.dispatched.sent == 1
    assert report.exit_code == 0


def test_an_unknown_confirmed_seen_is_closed_without_resending(tmp_path):
    s = store(tmp_path)
    p = pipeline_with_entry(s)
    moment = soon()
    Dispatcher(
        store=s, sender=Boom(), policy=Policy(live_submission_enabled=True)
    ).dispatch_once(state=p.state, now=moment, quotes=quotes(moment))

    report = service(s, lookup=Lookup("seen", "ord-9")).step(now=moment)
    assert report.reconciliation is not None
    assert report.reconciliation.sent == 1
    assert report.unknown_after == 0
    assert report.dispatched.attempted == 0, "a seen order must never be sent twice"
    assert s.outbox_snapshot()["counts"].get("SENT", 0) == 1


# ── the metrics series ──────────────────────────────────────────────────────
def test_the_series_records_each_tick_and_summarises(tmp_path):
    s = store(tmp_path)
    pipeline_with_entry(s)
    series_path = tmp_path / "metrics.jsonl"
    moment = soon()
    svc = service(s, series=series_path, quotes=quotes(moment))
    svc.step(now=moment)
    svc.step(now=moment)

    records = read_series(series_path)
    assert len(records) == 2
    summary = summarize_series(records)
    assert summary["ticks"] == 2
    assert summary["dispatched"]["sent"] == 1
    assert summary["last"]["dispatched"]["sent"] == 0


def test_the_series_is_bounded(tmp_path):
    path = tmp_path / "bounded.jsonl"
    log = SeriesLog(path, cap=3)
    for i in range(10):
        log.append({"ts_utc": f"t{i}", "tick": i})
    records = read_series(path)
    # The file oscillates between cap and 2*cap: bounded, and the newest survive.
    assert len(records) <= 2 * 3
    assert records[-1]["tick"] == 9


# ── cadence and backoff ─────────────────────────────────────────────────────
def test_idle_ticks_back_off_and_activity_resets(tmp_path):
    svc = service(
        store(tmp_path),
        config=ServiceConfig(cadence_seconds=1.0, jitter_seconds=0.0,
                             idle_backoff_seconds=2.0, max_backoff_seconds=8.0),
    )
    assert svc.next_delay(False) == 2.0
    assert svc.next_delay(False) == 4.0
    assert svc.next_delay(False) == 8.0
    assert svc.next_delay(False) == 8.0  # capped
    assert svc.next_delay(True) == 1.0  # activity resets the streak


def test_the_loop_stops_cleanly_at_max_ticks(tmp_path):
    s = store(tmp_path)
    svc = service(
        s,
        config=ServiceConfig(cadence_seconds=0.0, jitter_seconds=0.0,
                             idle_backoff_seconds=0.0, max_ticks=3),
    )
    assert svc.run(install_signals=False) == 0

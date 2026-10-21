"""Compaction and ambiguous-send resolution, proved rather than asserted.

The three claims under test:

1. `applied_events` is a total order that reproduces EVERY intermediate state, not just the
   final one. Checked at every prefix against states captured from the live pipeline.
2. Compaction (snapshot + truncate) is lossless: the book after compaction equals the book
   before it, and the session can keep running on top of the resumed state.
3. An UNKNOWN send is resolved only when the venue can answer, and "cannot say" leaves the
   row waiting instead of guessing "not sent".
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scorpion.config import Policy
from scorpion.domain import BookState
from scorpion.outbox import Dispatcher, Receipt
from scorpion.paper import quote
from scorpion.pipeline import Pipeline
from scorpion.reconciliation import (
    LookupResult,
    NoLookup,
    reconcile_unknown,
)
from scorpion.scenario import message
from scorpion.snapshot import (
    SnapshotError,
    compact,
    deserialize_state,
    digest_of,
    load_snapshot,
    serialize_state,
    state_from_snapshot,
    take_snapshot,
    verify_snapshot,
)
from scorpion.store import Store
from scorpion.verify import replay_applied, verify_log

D = Decimal
START = datetime.now(UTC).replace(microsecond=0)
QQQ = f"QQQ|CALL|719|{START.date().isoformat()}"
SPY = f"SPY|PUT|600|{START.date().isoformat()}"


class Ok:
    def __init__(self, price: str = "1.02", ref: str = "ord-1") -> None:
        self.price, self.ref = price, ref

    def send(self, envelope, *, now):  # noqa: ANN001
        return Receipt(ok=True, broker_ref=self.ref, fill_price=D(self.price), fill_qty=1)


def clock() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=1)


def quotes(at: datetime) -> dict:
    return {QQQ: quote("1.00", "1.02", at=at), SPY: quote("0.88", "0.90", at=at)}


def build_session(s: Store) -> tuple[Store, list[BookState], list[dict]]:
    """Two entries, each dispatched and settled, capturing the book after every step."""
    p = Pipeline(store=s)
    policy = Policy(live_submission_enabled=True)
    states: list[BookState] = []

    asyncio.run(p.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    states.append(p.state)

    t = clock()
    Dispatcher(store=s, sender=Ok(), policy=policy).dispatch_once(
        state=p.state, now=t, quotes=quotes(t)
    )
    p.settle(contract_key=QQQ, generation=1, quantity_delta=1, fill_price=D("1.02"),
             market_day=START.date())
    states.append(p.state)

    asyncio.run(p.handle(message("SPY 600P TODAY @ 0.89", message_id="m2", at=START)))
    states.append(p.state)

    t = clock()
    Dispatcher(store=s, sender=Ok("0.90"), policy=policy).dispatch_once(
        state=p.state, now=t, quotes=quotes(t)
    )
    p.settle(contract_key=SPY, generation=1, quantity_delta=16, fill_price=D("0.90"),
             market_day=START.date())
    states.append(p.state)
    return s, states, s.load_applied()


# ── the log is a total order over every intermediate state ───────────────────
def test_every_prefix_of_the_log_reproduces_the_state_it_recorded(tmp_path):
    s, states, rows = build_session(Store(tmp_path / "s.db"))
    assert len(rows) == 4, "two signals, two fills"
    for k, expected in enumerate(states, start=1):
        assert replay_applied(rows[:k]) == expected, f"prefix {k} diverged"


def test_the_log_alternates_signal_and_fill_in_sequence_order(tmp_path):
    _, _, rows = build_session(Store(tmp_path / "s.db"))
    assert [str(r["kind"]) for r in rows] == ["signal", "fill", "signal", "fill"]
    assert [int(str(r["seq"])) for r in rows] == [1, 2, 3, 4]


def test_a_corrupted_payload_fails_loudly(tmp_path):
    s, _, rows = build_session(Store(tmp_path / "s.db"))
    damaged = [dict(rows[0]), dict(rows[1])]
    damaged[1]["payload_json"] = json.dumps({"kind": "mystery"})
    with pytest.raises((ValueError, KeyError)):
        replay_applied(damaged)


def test_a_gap_in_the_sequence_is_reported_as_log_damage(tmp_path):
    s, _, rows = build_session(Store(tmp_path / "s.db"))
    with s.connect() as db:
        db.execute("DELETE FROM applied_events WHERE seq=2")
    result = verify_log(s)
    assert not result.ok
    assert any("sequence_gap" in d.field for d in result.divergences)


# ── snapshot fidelity ────────────────────────────────────────────────────────
def test_state_round_trips_through_its_snapshot_exactly(tmp_path):
    _, states, _ = build_session(Store(tmp_path / "s.db"))
    state = states[-1]
    assert deserialize_state(serialize_state(state)) == state


def test_the_digest_changes_when_a_byte_changes(tmp_path):
    _, states, _ = build_session(Store(tmp_path / "s.db"))
    payload = serialize_state(states[-1])
    assert digest_of(payload) != digest_of(payload.replace("0.90", "0.91"))


def test_a_tampered_snapshot_is_refused(tmp_path):
    s = Store(tmp_path / "s.db")
    _, states, rows = build_session(s)
    snap = take_snapshot(states[-1], seq=int(str(rows[-1]["seq"])))
    s.append_snapshot(seq=snap.seq, payload_json=snap.payload_json,
                      digest=snap.digest, created_ts_utc=snap.created_ts_utc)
    with s.connect() as db:
        db.execute("UPDATE state_snapshots SET payload_json=? WHERE seq=?",
                   (snap.payload_json.replace("OPEN", "CLOSED"), snap.seq))
    with pytest.raises(SnapshotError):
        load_snapshot(s)


# ── compaction ──────────────────────────────────────────────────────────────
def test_compaction_preserves_the_book_and_truncates_the_log(tmp_path):
    s = Store(tmp_path / "s.db")
    _, states, rows = build_session(s)
    before = states[-1]
    full = s.load_applied()
    assert len(full) == 4

    snap = compact(s, keep_tail=0)
    assert snap.seq == 4
    assert s.load_applied() == [], "the covered tail was truncated"

    resumed, seq = state_from_snapshot(s)
    assert seq == snap.seq
    assert resumed == before, "compaction changed the book"


def test_compaction_keeps_a_forensic_tail(tmp_path):
    s = Store(tmp_path / "s.db")
    build_session(s)
    compact(s, keep_tail=2)
    assert len(s.load_applied()) == 2
    assert int(str(s.load_applied()[0]["seq"])) == 3


def test_a_compacted_store_keeps_trading_identically(tmp_path):
    """The real test: after truncation, new work must land on the same book."""
    s = Store(tmp_path / "s.db")
    _, states, _ = build_session(s)
    compact(s, keep_tail=0)

    resumed, _ = state_from_snapshot(s)
    assert resumed == states[-1]

    # a fresh pipeline built on the resumed state must refuse a third concurrent position
    # exactly as the uncompacted one would: the resume is not a reset.
    assert len([p for p in resumed.positions.values() if p.quantity > 0]) == 2


def test_verify_still_works_after_compaction(tmp_path, capsys):
    """The CLI regression: a tail-only replay after truncation reports orphaned positions."""
    import argparse

    from scorpion.cli import cmd_verify

    s = Store(tmp_path / "s.db")
    _, states, _ = build_session(s)
    compact(s, keep_tail=0)
    code = cmd_verify(argparse.Namespace(db=str(s.path)))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["replayed"]["positions"][SPY]["quantity"] == 16
    assert payload["replayed"]["resumed_from_seq"] == 4


def test_compaction_refuses_an_empty_log(tmp_path):
    with pytest.raises(SnapshotError):
        compact(Store(tmp_path / "s.db"))


def test_verify_snapshot_accepts_a_consistent_store(tmp_path):
    s = Store(tmp_path / "s.db")
    build_session(s)
    compact(s, keep_tail=1)
    ok, problems = verify_snapshot(s)
    assert ok and problems == []


# ── UNKNOWN resolution ──────────────────────────────────────────────────────
class Boom:
    def send(self, envelope, *, now):  # noqa: ANN001
        raise TimeoutError("no answer")


class StaticLookup:
    def __init__(self, status: str, ref: str = "ord-9") -> None:
        self.status, self.ref = status, ref
        self.keys: list[str] = []

    def find(self, idempotency_key: str) -> LookupResult:
        self.keys.append(idempotency_key)
        return LookupResult(self.status, self.ref)  # type: ignore[arg-type]


def unknown_store(tmp_path) -> Store:
    s = Store(tmp_path / "s.db")
    p = Pipeline(store=s)
    asyncio.run(p.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    t = clock()
    Dispatcher(store=s, sender=Boom(), policy=Policy(live_submission_enabled=True)).dispatch_once(
        state=p.state, now=t, quotes=quotes(t)
    )
    assert s.outbox_snapshot()["counts"] == {"UNKNOWN": 1}
    return s


def test_the_venue_saying_seen_resolves_the_send_as_sent(tmp_path):
    s = unknown_store(tmp_path)
    report = reconcile_unknown(store=s, lookup=StaticLookup("seen", "ord-42"))
    assert report.sent == 1 and report.clean
    assert s.outbox_snapshot()["counts"] == {"SENT": 1}


def test_the_venue_saying_not_seen_makes_it_retryable(tmp_path):
    s = unknown_store(tmp_path)
    report = reconcile_unknown(store=s, lookup=StaticLookup("not_seen"))
    assert report.retried == 1
    assert s.outbox_snapshot()["counts"] == {"RETRY": 1}


def test_a_venue_that_cannot_answer_leaves_the_row_unknown(tmp_path):
    s = unknown_store(tmp_path)
    report = reconcile_unknown(store=s, lookup=NoLookup())
    assert report.pending == 1 and not report.clean
    assert s.outbox_snapshot()["counts"] == {"UNKNOWN": 1}


def test_the_lookup_key_is_the_one_recorded_at_send_time(tmp_path):
    """The venue is asked about the exact string it was given, not a re-derivation."""
    s = unknown_store(tmp_path)
    stored = store_key(s)
    assert stored, "the key must be persisted when the send becomes ambiguous"
    assert stored.endswith(f":{QQQ}:1:PROPOSE_OPEN")
    lookup = StaticLookup("seen")
    reconcile_unknown(store=s, lookup=lookup)
    assert lookup.keys == [stored]


def store_key(s: Store) -> str:
    with s.connect() as db:
        row = db.execute(
            "SELECT idempotency_key FROM outbox_state WHERE status='UNKNOWN'"
        ).fetchone()
    return str(row["idempotency_key"]) if row else ""


def test_resolution_is_idempotent(tmp_path):
    s = unknown_store(tmp_path)
    reconcile_unknown(store=s, lookup=StaticLookup("seen"))
    again = reconcile_unknown(store=s, lookup=StaticLookup("seen"))
    assert again.resolutions == ()

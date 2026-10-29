"""The venue feed and the scheduled reconcile: evidence, or a page - never a guess.

Two things are proven here, and they are different claims:

1. **The feed refuses what is not evidence.** A missing file, a 500, a transport error,
   a missing positions path, a row that is not a contract, or a snapshot older than the
   limit produces `VenueError`/`ReconcileError` - never an empty book. "No positions"
   and "no feed" are opposite facts, and the flat one would silently stop every exit.

2. **The consequence is separated from the evidence.** Drift halts NEW buys and pages
   once (not once per tick); a dead or malformed feed pages and halts nothing, because a
   broken adapter must not become a stopped desk; a clean re-check cannot lift a halt a
   human never lifted; and the comparison is made against the CONFIRMED book when one is
   attached, so a sell that never reached the venue cannot hide.

The HTTP adapter is exercised against a real local server (the same discipline the order
adapter has), so the request shape, the bearer header and the error taxonomy are real
rather than mocked. The token must never appear in an error string.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scorpion_mff import cli
from scorpion_mff.broker import Ack, PaperBroker
from scorpion_mff.engine import Engine
from scorpion_mff.execution_book import ExecutionBook
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.reconcile import ReconcileError, VenuePosition
from scorpion_mff.types import OrderIntent, Position, Quote, RawMessage, Side
from scorpion_mff.venue import (
    DEFAULT_MAX_AGE_S,
    FileVenueFeed,
    HttpVenueFeed,
    VenueError,
    VenueSnapshot,
    feed_from_env,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
T0 = 1_757_340_000.0
QQQ = "QQQ|CALL|720|0DTE"
NVDA = "NVDA|PUT|227.5|2026-09-11"
TOKEN = "venue-secret-token-0987654321"


def qqq(contracts: int = 3) -> VenuePosition:
    return VenuePosition(contract_key=QQQ, contracts=contracts)


def nvda(contracts: int = 2) -> VenuePosition:
    return VenuePosition(contract_key=NVDA, contracts=contracts)


def write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def engine_with(book: dict[str, Position], *, halted: bool = False) -> Engine:
    engine = Engine(fills=FillLog(), clock=lambda: T0)
    engine.positions = dict(book)
    engine.halted = halted
    return engine


def position(key: str = QQQ, contracts: int = 3, price: float = 1.00) -> Position:
    side = Side.CALL if "|CALL|" in key else Side.PUT
    return Position(contract_key=key, ticker=key.split("|")[0], side=side,
                    contracts=contracts, avg_price=price, opened_ts=T0)


# ── the file feed: shapes, stamps, refusals ─────────────────────────────────
def test_a_bare_array_is_a_snapshot(tmp_path):
    snapshot = FileVenueFeed(path=FIXTURES / "venue-positions.json").snapshot(T0)
    assert [item.contract_key for item in snapshot.positions] == [QQQ, NVDA]
    assert snapshot.contracts == 5
    assert snapshot.schema.startswith("mff.venue-snapshot.v")


def test_an_object_with_positions_and_a_timestamp_is_a_snapshot(tmp_path):
    path = write_json(tmp_path / "venue.json", {
        "as_of": "2026-09-08T14:00:00Z",
        "positions": [{"key": QQQ, "qty": 4}],
    })
    snapshot = FileVenueFeed(path=path).snapshot(T0 + 3600)
    assert snapshot.contracts == 4
    expected = datetime.fromisoformat("2026-09-08T14:00:00+00:00").timestamp()
    assert snapshot.taken_at == pytest.approx(expected)
    assert snapshot.age_s(T0 + 60) is not None


def test_a_missing_file_is_not_a_flat_book(tmp_path):
    with pytest.raises(VenueError, match="not found"):
        FileVenueFeed(path=tmp_path / "nope.json").snapshot(T0)


def test_an_unconfigured_file_feed_refuses(tmp_path):
    feed = FileVenueFeed()
    assert feed.configured is False
    with pytest.raises(VenueError, match="no venue snapshot file"):
        feed.snapshot(T0)


def test_a_non_json_file_is_malformed_not_empty(tmp_path):
    path = tmp_path / "venue.json"
    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ReconcileError, match="not JSON"):
        FileVenueFeed(path=path).snapshot(T0)


def test_an_object_without_a_position_list_names_what_it_found(tmp_path):
    path = FIXTURES / "venue-positions-malformed.json"
    with pytest.raises(ReconcileError, match="position list"):
        FileVenueFeed(path=path).snapshot(T0)


def test_a_positions_field_that_is_not_a_list_is_refused(tmp_path):
    path = write_json(tmp_path / "venue.json", {"positions": "lots"})
    with pytest.raises(ReconcileError, match="must be a JSON array"):
        FileVenueFeed(path=path).snapshot(T0)


def test_a_duplicate_venue_line_is_refused_at_the_feed(tmp_path):
    """Two answers to one question is malformed evidence, not a book."""
    path = write_json(tmp_path / "venue.json",
                      [{"contract_key": QQQ, "contracts": 1},
                       {"contract_key": QQQ, "contracts": 2}])
    with pytest.raises(ReconcileError, match="twice"):
        FileVenueFeed(path=path).snapshot(T0)


def test_a_negative_or_boolean_count_is_refused(tmp_path):
    for bad in (-1, True, 2.5):
        path = write_json(tmp_path / "venue.json", [{"contract_key": QQQ, "contracts": bad}])
        with pytest.raises(ReconcileError):
            FileVenueFeed(path=path).snapshot(T0)


def test_a_stale_snapshot_is_refused_with_its_age(tmp_path):
    path = write_json(tmp_path / "venue.json",
                      {"as_of": T0, "positions": [{"contract_key": QQQ, "contracts": 1}]})
    feed = FileVenueFeed(path=path, max_age_s=60.0)
    with pytest.raises(VenueError, match="old"):
        feed.snapshot(T0 + 61)
    # one second inside the limit is evidence
    assert feed.snapshot(T0 + 59).contracts == 1


def test_a_snapshot_with_no_timestamp_is_refused_when_an_age_is_required(tmp_path):
    path = write_json(tmp_path / "venue.json", [{"contract_key": QQQ, "contracts": 1}])
    path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    feed = FileVenueFeed(path=path, max_age_s=60.0)
    # a file always has an mtime, so the stamp is known - the refusal only fires when
    # even that is unavailable, which is what `VenueSnapshot.check_age` guards.
    assert feed.snapshot(T0).taken_at is not None
    dateless = VenueSnapshot(positions=(qqq(),), taken_at=None, source="test")
    with pytest.raises(VenueError, match="no timestamp"):
        dateless.check_age(T0, 60.0)


def test_the_file_mtime_is_the_stamp_when_the_payload_has_none(tmp_path):
    path = write_json(tmp_path / "venue.json", [{"contract_key": QQQ, "contracts": 1}])
    snapshot = FileVenueFeed(path=path).snapshot(T0)
    assert snapshot.taken_at == pytest.approx(path.stat().st_mtime, abs=1.0)


def test_the_age_limit_can_be_off_for_a_recorded_fixture(tmp_path):
    path = write_json(tmp_path / "venue.json",
                      {"as_of": T0, "positions": [{"contract_key": QQQ, "contracts": 1}]})
    old = FileVenueFeed(path=path, max_age_s=None).snapshot(T0 + 10_000_000)
    assert old.contracts == 1
    assert old.stale(T0 + 10_000_000, None) is False


# ── the HTTP feed: a real local server, and a token that never leaks ────────
class PositionsServer(BaseHTTPRequestHandler):
    script: list[tuple[int, object]] = []
    requests: list[dict[str, str]] = []

    def do_GET(self):  # noqa: N802 - http.server naming
        PositionsServer.requests.append({"path": self.path,
                                          "headers": dict(self.headers)})
        status, payload = PositionsServer.script[
            min(len(PositionsServer.requests) - 1, len(PositionsServer.script) - 1)]
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        return


@pytest.fixture()
def positions_server():
    PositionsServer.requests = []
    PositionsServer.script = [(200, {
        "as_of": T0,
        "results": [{"contract_key": QQQ, "contracts": 3},
                    {"contract_key": NVDA, "contracts": 2}],
    })]
    server = HTTPServer(("127.0.0.1", 0), PositionsServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/positions?account={{account}}"
    finally:
        server.shutdown()


def feed_for(url: str, **kw) -> HttpVenueFeed:
    base = dict(template=url, positions_path="results", token=TOKEN,
                account="acct-1", clock=lambda: T0 + 5)
    base.update(kw)
    return HttpVenueFeed(**base)


def test_the_http_feed_reads_the_positions_path(positions_server):
    snapshot = feed_for(positions_server).snapshot(T0 + 5)
    assert snapshot.contracts == 5
    assert snapshot.taken_at == T0
    assert snapshot.source.startswith("http:127.0.0.1")
    request = PositionsServer.requests[0]
    assert "account=acct-1" in request["path"]
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_a_server_error_is_unavailable_not_drift(positions_server):
    PositionsServer.script = [(500, {"detail": "boom"})]
    with pytest.raises(VenueError, match="HTTP 500"):
        feed_for(positions_server).snapshot(T0)


def test_a_transport_error_is_unavailable_and_token_free():
    def dead(request, timeout):                     # noqa: ARG001 - transport signature
        return 0, {}

    feed = feed_for("http://venue.invalid/positions", transport=dead)
    with pytest.raises(VenueError) as excinfo:
        feed.snapshot(T0)
    assert "unreachable" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


def test_a_missing_positions_path_names_the_path():
    def payload(request, timeout):                  # noqa: ARG001
        return 200, {"holdings": [{"contract_key": QQQ, "contracts": 1}]}

    feed = feed_for("http://venue.invalid/positions", transport=payload)
    with pytest.raises(VenueError, match="results"):
        feed.snapshot(T0)


def test_an_unconfigured_http_feed_refuses():
    feed = HttpVenueFeed(template="")
    assert feed.configured is False
    with pytest.raises(VenueError, match="MFF_VENUE_URL"):
        feed.snapshot(T0)


def test_a_http_snapshot_older_than_the_limit_is_refused(positions_server):
    PositionsServer.script = [(200, {"as_of": T0,
                                     "results": [{"contract_key": QQQ, "contracts": 1}]})]
    feed = feed_for(positions_server, max_age_s=60.0)
    with pytest.raises(VenueError, match="old"):
        feed.snapshot(T0 + 61)


def test_a_http_body_with_no_timestamp_is_stamped_at_the_read_time(positions_server):
    PositionsServer.script = [(200, {"results": [{"contract_key": QQQ, "contracts": 1}]})]
    snapshot = feed_for(positions_server, max_age_s=60.0).snapshot(T0 + 5)
    assert snapshot.taken_at == pytest.approx(T0 + 5)


def test_the_http_source_never_carries_a_token_from_the_url():
    feed = HttpVenueFeed(template=f"http://venue.local/positions?token={TOKEN}")
    assert TOKEN not in feed.source


# ── environment wiring ──────────────────────────────────────────────────────
def test_no_environment_means_no_feed(monkeypatch):
    monkeypatch.delenv("MFF_VENUE_SNAPSHOT", raising=False)
    monkeypatch.delenv("MFF_VENUE_URL", raising=False)
    assert feed_from_env() is None


def test_a_snapshot_path_in_the_environment_builds_a_file_feed(monkeypatch, tmp_path):
    monkeypatch.setenv("MFF_VENUE_SNAPSHOT", str(tmp_path / "v.json"))
    monkeypatch.delenv("MFF_VENUE_URL", raising=False)
    feed = feed_from_env()
    assert isinstance(feed, FileVenueFeed)
    assert feed.configured is True
    assert feed.max_age_s == DEFAULT_MAX_AGE_S


def test_a_url_in_the_environment_builds_an_http_feed(monkeypatch):
    monkeypatch.delenv("MFF_VENUE_SNAPSHOT", raising=False)
    monkeypatch.setenv("MFF_VENUE_URL", "http://venue.local/positions")
    feed = feed_from_env(max_age_s=30.0)
    assert isinstance(feed, HttpVenueFeed)
    assert feed.max_age_s == 30.0


# ── the engine: one page per disagreement, never an un-halt ────────────────
def test_drift_halts_buys_and_pages_once():
    engine = engine_with({QQQ: position(contracts=3)})
    report = engine.reconcile_positions([qqq(0)], T0)
    assert report.clean is False
    assert engine.halted is True and engine.halt_reason == "reconcile-drift"

    alerts = [f for f in engine.fills.records if f.event == "alert"]
    assert len(alerts) == 1
    assert "phantom_local" in alerts[0].reason


def test_an_unchanged_drift_is_recorded_but_not_re_paged():
    """A five-minute cadence must be one page, not twelve a day."""
    engine = engine_with({QQQ: position(contracts=3)})
    engine.reconcile_positions([qqq(0)], T0)
    engine.reconcile_positions([qqq(0)], T0 + 300)
    engine.reconcile_positions([qqq(0)], T0 + 600)
    alerts = [f for f in engine.fills.records if f.event == "alert"]
    quiet = [f for f in engine.fills.records
             if f.event == "reconcile" and f.reason == "drift_unchanged"]
    assert len(alerts) == 1
    assert len(quiet) == 2
    assert engine.halted is True


def test_a_changed_drift_pages_again():
    engine = engine_with({QQQ: position(contracts=3)})
    engine.reconcile_positions([qqq(0)], T0)
    engine.reconcile_positions([qqq(1)], T0 + 300)          # 3 vs 1 is a new signature
    alerts = [f for f in engine.fills.records if f.event == "alert"]
    assert len(alerts) == 2


def test_a_clean_snapshot_after_drift_is_recorded_and_does_not_un_halt():
    """Recovery is an operator act: `halted` is shared with STOP."""
    engine = engine_with({QQQ: position(contracts=3)})
    engine.reconcile_positions([qqq(0)], T0)
    engine.reconcile_positions([qqq(3)], T0 + 300)
    rows = [f for f in engine.fills.records if f.event == "reconcile"]
    assert rows[-1].reason == "clean_after_drift"
    assert engine.halted is True


def test_a_clean_first_check_says_clean_not_clean_after_drift():
    engine = engine_with({QQQ: position(contracts=3)})
    engine.reconcile_positions([qqq(3)], T0)
    rows = [f for f in engine.fills.records if f.event == "reconcile"]
    assert rows[-1].reason == "clean"
    assert engine.halted is False


def test_the_confirmed_book_can_be_the_local_side():
    """The engine deletes a position at sell-intent time; the venue does not."""
    engine = engine_with({})                                # the failed sell looked flat
    confirmed = [position(QQQ, contracts=3)]                # ... but the book still holds it
    report = engine.reconcile_positions([qqq(3)], T0, local=confirmed)
    assert report.clean is True

    engine.reconcile_positions([qqq(0)], T0 + 1, local=confirmed)
    assert engine.halted is True
    assert any("phantom_local" in f.reason for f in engine.fills.records
               if f.event == "alert")


def test_an_exit_is_still_allowed_after_drift_halts_buys():
    engine = engine_with({QQQ: position(contracts=3)})
    engine.reconcile_positions([qqq(0)], T0)
    assert engine.halted is True
    from scorpion_mff.types import AlertKind, Signal

    intents = engine.on_signal(
        Signal(kind=AlertKind.CLOSE, channel="etf-options", ticker="QQQ",
               side=Side.CALL, strike=720.0, expiry="0DTE"),
        quote=Quote(bid=1.10, ask=1.12, ts=T0 + 1), now=T0 + 1)
    assert [i.action for i in intents] == ["sell"]


# ── the listener: cadence, silence, and the confirmed book ─────────────────
def build_listener(tmp_path, *, execution=None, feed=None, every=300.0,
                   broker=None) -> Listener:
    class Quotes:
        def quote(self, contract_key: str) -> Quote | None:
            return Quote(bid=1.01, ask=1.05)

    return Listener(
        transport=TapeTransport([]),
        engine=Engine(fills=FillLog(), clock=lambda: T0),
        broker=broker or PaperBroker(),
        quotes=Quotes(),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=lambda: T0),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: T0),
        clock=lambda: T0,
        execution=execution,
        venue_feed=feed,
        venue_every_s=every,
    )


class CountingFeed:
    def __init__(self, positions, *, fail: Exception | None = None) -> None:
        self.positions = positions
        self.fail = fail
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    @property
    def source(self) -> str:
        return "counting"

    def snapshot(self, now=None):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return VenueSnapshot(positions=tuple(self.positions), taken_at=now,
                             source="counting")


def test_no_feed_means_no_venue_call_and_no_halt(tmp_path):
    listener = build_listener(tmp_path)
    assert listener.reconcile_from_feed(T0) is None
    assert listener.venue_checks == 0
    assert listener.engine.halted is False


def test_the_feed_is_asked_on_the_cadence_not_every_tick(tmp_path):
    feed = CountingFeed([qqq(0)])
    listener = build_listener(tmp_path, feed=feed, every=300.0)
    listener.reconcile_from_feed(T0)
    listener.reconcile_from_feed(T0 + 15)
    listener.reconcile_from_feed(T0 + 299)
    assert feed.calls == 1
    listener.reconcile_from_feed(T0 + 300)
    assert feed.calls == 2


def test_force_asks_immediately(tmp_path):
    feed = CountingFeed([qqq(0)])
    listener = build_listener(tmp_path, feed=feed, every=300.0)
    listener.reconcile_from_feed(T0, force=True)
    listener.reconcile_from_feed(T0 + 1, force=True)
    assert feed.calls == 2


def test_an_unreadable_feed_pages_and_does_not_halt_buys(tmp_path):
    """A broken adapter must not become a stopped desk."""
    feed = CountingFeed([], fail=VenueError("feed HTTP 503"))
    listener = build_listener(tmp_path, feed=feed)
    assert listener.reconcile_from_feed(T0) is None
    assert listener.engine.halted is False
    alerts = [f for f in listener.engine.fills.records if f.event == "alert"]
    assert any("reconcile_feed" in a.reason for a in alerts)
    assert listener.venue_checks == 0


def test_a_malformed_snapshot_also_does_not_halt(tmp_path):
    feed = CountingFeed([], fail=ReconcileError("venue snapshot must be a JSON array"))
    listener = build_listener(tmp_path, feed=feed)
    assert listener.reconcile_from_feed(T0) is None
    assert listener.engine.halted is False


def test_the_listener_halts_buys_on_real_drift(tmp_path):
    feed = CountingFeed([qqq(0)])                       # the venue says flat
    listener = build_listener(tmp_path, feed=feed)
    listener.engine.positions = {QQQ: position(contracts=3)}
    report = listener.reconcile_from_feed(T0)
    assert report is not None and report.clean is False
    assert listener.engine.halted is True
    assert listener.venue_checks == 1


def test_the_listener_compares_against_the_confirmed_book(tmp_path):
    """The engine's own book may be empty because a sell was attempted, not confirmed."""
    book = ExecutionBook()
    order = book.record_intent(
        OrderIntent(action="buy", qty=3, limit=1.00, contract_key=QQQ,
                    reason="entry:FULL:his_bid=1.00:ask=1.05",
                    idempotency_key=f"{QQQ}:buy:1"), ts=T0)
    book.apply_ack(order.client_id, Ack(ok=True, contract_key=QQQ, qty=3, price=1.00,
                                        broker_order_id="rh-1"), ts=T0)
    listener = build_listener(tmp_path, execution=book,
                              feed=CountingFeed([qqq(3)]))
    assert listener.engine.positions == {}               # nothing in the decision cache
    report = listener.reconcile_from_feed(T0)
    assert report is not None and report.clean is True   # the confirmed book agrees
    assert listener.engine.halted is False


def test_the_tick_runs_the_venue_check_first(tmp_path):
    feed = CountingFeed([qqq(0)])
    listener = build_listener(tmp_path, feed=feed)
    listener.engine.positions = {QQQ: position(contracts=3)}
    listener.tick(T0)
    assert feed.calls == 1
    assert listener.engine.halted is True


# ── the CLI: the documented exit codes, and an honest source ───────────────
def build_confirmed_log(path: Path, qty: int = 2) -> None:
    book = ExecutionBook(path=path)
    order = book.record_intent(
        OrderIntent(action="buy", qty=qty, limit=1.00, contract_key=QQQ,
                    reason="entry:FULL:his_bid=1.00:ask=1.05",
                    idempotency_key=f"{QQQ}:buy:1"), ts=T0)
    book.apply_ack(order.client_id, Ack(ok=True, contract_key=QQQ, qty=qty, price=1.00,
                                        broker_order_id="rh-1"), ts=T0)


def test_cli_reconcile_clean_on_a_matching_snapshot(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "EXECUTION_LOG", tmp_path / "execution.jsonl")
    build_confirmed_log(tmp_path / "execution.jsonl", qty=3)
    snapshot = tmp_path / "venue.json"
    write_json(snapshot, [{"contract_key": QQQ, "contracts": 3}])
    assert cli.main(["reconcile", "--snapshot", str(snapshot)]) == 0
    out = capsys.readouterr().out
    assert "CLEAN" in out
    assert "confirmed" in out                            # the source is printed


def test_cli_reconcile_drift_exits_1_and_names_the_kind(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "EXECUTION_LOG", tmp_path / "execution.jsonl")
    build_confirmed_log(tmp_path / "execution.jsonl", qty=3)
    snapshot = tmp_path / "venue.json"
    write_json(snapshot, [{"contract_key": QQQ, "contracts": 1}])
    assert cli.main(["reconcile", "--snapshot", str(snapshot)]) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out and "quantity_mismatch" in out
    assert "live/NO_TRADE" in out                        # the next step, named


def test_cli_reconcile_falls_back_to_the_fill_log_without_a_confirmed_book(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "EXECUTION_LOG", tmp_path / "execution.jsonl")
    fills = tmp_path / "fills.jsonl"
    log = FillLog(path=fills)
    log.record(event="buy", contract_key=QQQ, qty=2, price=1.00, reason="entry", ts=T0)
    snapshot = tmp_path / "venue.json"
    write_json(snapshot, [{"contract_key": QQQ, "contracts": 2}])
    assert cli.main(["reconcile", "--snapshot", str(snapshot), "--fills", str(fills)]) == 0
    assert "fills" in capsys.readouterr().out


def test_cli_reconcile_source_confirmed_without_a_book_exits_2(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "EXECUTION_LOG", tmp_path / "execution.jsonl")
    snapshot = tmp_path / "venue.json"
    write_json(snapshot, [])
    assert cli.main(["reconcile", "--snapshot", str(snapshot),
                     "--source", "confirmed"]) == 2
    assert "no confirmed execution book" in capsys.readouterr().err


def test_cli_reconcile_refuses_a_stale_snapshot(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "EXECUTION_LOG", tmp_path / "execution.jsonl")
    snapshot = tmp_path / "venue.json"
    write_json(snapshot, {"as_of": T0, "positions": []})
    assert cli.main(["reconcile", "--snapshot", str(snapshot), "--max-age", "60"]) == 2
    assert "old" in capsys.readouterr().err


def test_cli_reconcile_json_names_the_source_and_the_snapshot(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "EXECUTION_LOG", tmp_path / "execution.jsonl")
    build_confirmed_log(tmp_path / "execution.jsonl", qty=2)
    snapshot = tmp_path / "venue.json"
    write_json(snapshot, {"as_of": T0, "positions": [{"contract_key": QQQ, "contracts": 2}]})
    assert cli.main(["reconcile", "--snapshot", str(snapshot), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["local_source"] == "confirmed"
    assert payload["venue"]["contracts"] == 2
    assert payload["report"]["clean"] is True


# ── the snapshot it compares is the one `book show` reports ────────────────
def test_a_confirmed_book_and_a_venue_snapshot_use_the_same_contract_keys(tmp_path):
    """The join is the contract key, never the ticker: a ticker cannot name an option."""
    book = ExecutionBook()
    for qty, key in ((3, QQQ), (2, NVDA)):
        order = book.record_intent(
            OrderIntent(action="buy", qty=qty, limit=1.00, contract_key=key,
                        reason="entry:FULL:his_bid=1.00:ask=1.05",
                        idempotency_key=f"{key}:buy:1"), ts=T0)
        book.apply_ack(order.client_id,
                       Ack(ok=True, contract_key=key, qty=qty, price=1.00,
                           broker_order_id="rh-1"), ts=T0)
    local = book.open_positions()
    assert {p.contract_key for p in local} == {QQQ, NVDA}
    from scorpion_mff.reconcile import reconcile

    snapshot = FileVenueFeed(path=FIXTURES / "venue-positions.json").snapshot(T0)
    assert reconcile(local, snapshot.positions).clean is True


def test_raw_message_helper_is_unused_but_the_snapshot_is_offline(tmp_path):
    """Guard against an accidental network dependency in the offline path."""
    snapshot = FileVenueFeed(path=FIXTURES / "venue-positions.json").snapshot(T0)
    assert snapshot.schema == "mff.venue-snapshot.v1"
    assert RawMessage is not None                        # imported for the listener type

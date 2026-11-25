"""Conservative equity quote-depth replay from the existing intent journal.

This is a distinct observational execution model, not the frozen OHLC return
fixture and not a broker. No queue priority, NBBO, fill confirmation or live
execution is inferred. Replays are deterministic and side-effect free.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from .causal_features import (
    _paper_decimal,
    _paper_int,
    _paper_text,
    _paper_timestamp,
    paper_features_from_records,
)
from .config import FORWARD_RULES_SHA256, MARKET_TZ, STALE_POLICY, load_forward_rules
from .execution_model import EntrySchedule, strict_paper_schedule
from .provenance import stable_hash
from .session_calendar import session_hours
from .session_service import (
    MAX_PAPER_JOURNAL_BYTES,
    decode_paper_record,
    journal_identity_bytes,
    read_verified_paper_records,
)

MODEL = "equity-displayed-depth-v1"
_MAX_BYTES = MAX_PAPER_JOURNAL_BYTES


@dataclass(frozen=True, slots=True)
class ModeledFill:
    call_id: str
    ticker: str
    role: str
    side: str
    quantity: int
    price: Decimal
    intent_seq: int
    market_seq: int
    market_event_ns: int
    authority: str = "MODELED_PAPER_ONLY"


@dataclass(frozen=True, slots=True)
class ModeledCall:
    call_id: str
    ticker: str
    initial_target: int
    bought: int
    sold: int
    open_quantity: int
    buy_notional_usd: Decimal
    sell_notional_usd: Decimal
    gross_round_trip_usd: Decimal | None
    assumed_round_trip_cost_usd: Decimal | None
    modeled_net_round_trip_usd: Decimal | None
    initial_observations: int
    chase_observations: int
    exit_observations: int
    state: str


@dataclass(frozen=True, slots=True)
class EquityPaperReport:
    journal_sha256: str
    input_kind: str
    model: str
    market_dataset: str
    market_schema: str
    modeled_fills: tuple[ModeledFill, ...]
    calls: tuple[ModeledCall, ...]
    market_records: int
    open_inventory: tuple[tuple[str, int], ...]
    modeled_net_round_trip_usd: Decimal | None
    blockers: tuple[str, ...]
    report_sha256: str
    authority: str = "RESEARCH_ONLY"
    certifies_venue_fills: bool = False
    resume_authorized: bool = False
    authorizes_live_orders: bool = False


@dataclass(slots=True)
class _Order:
    seq: int
    role: str
    submitted_ns: int
    deadline_ns: int
    quantity: int
    limit: Decimal | None
    filled: int = 0
    observations: int = 0


@dataclass(slots=True)
class _Call:
    ticker: str
    schedule: EntrySchedule
    target: int
    initial: _Order
    chase: _Order | None = None
    exit: _Order | None = None
    bought: int = 0
    sold: int = 0
    buy_cash: Decimal = Decimal(0)
    sell_cash: Decimal = Decimal(0)
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Liquidity:
    event_ns: int
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    bid_remaining: int
    ask_remaining: int
    publisher: int
    instrument: int


def _ns(moment: datetime) -> int:
    return int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1000


def _depth(previous: _Liquidity | None, event: int, bid: Decimal, ask: Decimal,
           bid_size: int, ask_size: int, publisher: int, instrument: int) -> _Liquidity:
    # A fresh timestamp alone is not a fresh queue. At an unchanged price, only
    # positive displayed-size increments can add to unconsumed modeled liquidity.
    def available(old_price: Decimal, new_price: Decimal, old_size: int,
                  new_size: int, remaining: int) -> int:
        if old_price != new_price:
            return new_size
        return min(new_size, remaining + max(0, new_size - old_size))

    if previous is None:
        return _Liquidity(event, bid, ask, bid_size, ask_size, bid_size, ask_size,
                          publisher, instrument)
    if (previous.publisher, previous.instrument) != (publisher, instrument):
        raise ValueError("market instrument identity changed")
    if event <= previous.event_ns:
        raise ValueError("market event order conflict")
    return _Liquidity(
        event, bid, ask, bid_size, ask_size,
        available(previous.bid, bid, previous.bid_size, bid_size, previous.bid_remaining),
        available(previous.ask, ask, previous.ask_size, ask_size, previous.ask_remaining),
        publisher, instrument,
    )


def _read_prefix(folder: Path) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    def bounded(path: Path, limit: int) -> bytes:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("recovered evidence bound exceeded")
        return raw

    manifest = json.loads(bounded(folder / "recovery.json", 65536))
    prefix = bounded(folder / "verified-prefix.jsonl", _MAX_BYTES)
    tail = bounded(folder / "unverified-tail.bin", _MAX_BYTES - len(prefix))
    checkpoint = bounded(folder / "source-checkpoint.json", 65536)
    identity = journal_identity_bytes(prefix)
    if (
        manifest.get("schema") != "scorpion.paper-journal-recovery.v1"
        or manifest.get("authority") != "RESEARCH_ONLY"
        or manifest.get("status") not in ("SEALED_COPY", "VERIFIED_PREFIX_QUARANTINED")
        or manifest.get("resume_authorized") is not False
        or manifest.get("verified_prefix") != identity or json.loads(checkpoint) != identity
        or manifest.get("source_sha256") != hashlib.sha256(prefix + tail).hexdigest()
        or manifest.get("checkpoint_sha256") != hashlib.sha256(checkpoint).hexdigest()
        or manifest.get("unverified_tail_sha256") != hashlib.sha256(tail).hexdigest()
        or manifest.get("unverified_tail_bytes") != len(tail)
    ):
        raise ValueError("recovered evidence identity mismatch")
    return identity, tuple(decode_paper_record(line) for line in prefix.splitlines())


def replay_equity_market_journal(
    path: Path, *, recovered: bool = False,
) -> EquityPaperReport:
    """Replay sealed capture or quarantined durable prefix, never resume broker effects."""
    receipt, rows = _read_prefix(path) if recovered else read_verified_paper_records(path)
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        return _replay(receipt, rows, recovered=recovered)


def _replay(
    receipt: dict[str, object], rows: tuple[dict[str, object], ...], *, recovered: bool,
) -> EquityPaperReport:
    rules = load_forward_rules()
    if rules.sha256 != FORWARD_RULES_SHA256:
        raise ValueError("frozen rules identity mismatch")
    if not rows or rows[0].get("kind") != "session_start":
        raise ValueError("market capture lifecycle missing")
    start = rows[0]
    day = datetime.fromisoformat(_paper_text(start.get("session_date"))).date()
    hours = session_hours(day)
    if hours is None:
        raise ValueError("capture outside regular market day")
    if (
        start.get("market_capture_enabled") is not True or start.get("mode") != "PAPER"
        or start.get("rules_sha256") != rules.sha256
        or start.get("market_dataset") != "XNAS.ITCH" or start.get("market_schema") != "mbp-1"
    ):
        raise ValueError("unsupported market capture scope")
    features = paper_features_from_records(receipt, rows)
    if features.refusals:
        raise ValueError("invalid captured intent evidence")
    by_seq = {row.intent_seq: row for row in features.rows}
    calls: dict[str, _Call] = {}
    books: dict[str, _Liquidity] = {}
    watermarks: dict[str, int] = {}
    fills: list[ModeledFill] = []
    blockers: set[str] = set()
    halted = False
    market_count = 0
    last_wall = last_mono = -1
    last_record: datetime | None = None
    if recovered:
        blockers.add("recovered_prefix_not_complete_session")
    if start.get("latch_armed") is not True:
        blockers.add("unarmed_at_start")
        halted = True
    if _paper_timestamp(start.get("ts_utc")) > hours[0]:
        blockers.add("late_session_start")
    close_ns = _ns(hours[1])

    for row in rows:
        kind = row.get("kind")
        seq = _paper_int(row.get("seq"))
        stamp = _paper_timestamp(row.get("ts_utc"))
        if last_record is not None and stamp < last_record:
            raise ValueError("journal wall clock regression")
        last_record = stamp
        if stamp.astimezone(MARKET_TZ).date() != day:
            raise ValueError("journal session date mismatch")
        if (
            kind in ("market_gap", "latch")
            or row.get("action") == "blocked:conflicting_signal_create"
            or row.get("reason") == "blocked:no_trade_latch"
        ):
            blockers.add("market_or_safety_continuity_lost")
            halted = True
            books.clear()
        if kind in ("market_quote", "market_gap", "market_withdrawal"):
            market_count += 1
        if kind == "market_withdrawal":
            symbol = _paper_text(row.get("symbol"))
            # Do not reinitialize consumed size after a withdrawal at an unknown book state.
            blockers.add("market_withdrawal_observed")
            halted = True
            books.pop(symbol, None)
        if kind == "intent":
            feature = by_seq[seq]
            if feature.rules_sha256 != rules.sha256:
                raise ValueError("intent rules mismatch")
            source = _paper_timestamp(feature.features["source_ts_utc"])
            seen = _paper_timestamp(feature.features["received_ts_utc"])
            schedule = strict_paper_schedule(
                source, seen, rules, session_open=hours[0], session_close=hours[1],
            )
            expected = (schedule.entry_ts, schedule.chase_ts, schedule.exit_ts)
            declared = tuple(_paper_timestamp(row.get(key)) for key in (
                "scheduled_entry_ts_utc", "scheduled_chase_ts_utc", "scheduled_exit_ts_utc",
            ))
            if expected != declared:
                raise ValueError("intent schedule mismatch")
            chase = feature.role == "CHASE"
            due = schedule.chase_ts if chase else schedule.entry_ts
            if not due <= stamp <= due + timedelta(seconds=3):
                raise ValueError("intent outside timer budget")
            qty = _paper_int(row.get("quantity"))
            limit = _paper_decimal(row.get("limit"))
            if qty * limit > rules.paper_notional_usd:
                raise ValueError("intent exceeds paper notional")
            key = feature.call_id
            if not chase:
                if key in calls:
                    raise ValueError("duplicate initial intention")
                calls[key] = _Call(
                    feature.ticker, schedule, qty,
                    _Order(seq, "INITIAL", _ns(stamp), _ns(schedule.chase_ts), qty, limit),
                )
            else:
                call = calls.get(key)
                if call is None or call.chase is not None or call.ticker != feature.ticker:
                    raise ValueError("conditional chase identity mismatch")
                if call.bought:
                    # An initial partial is also a fill. Never double the parent entry.
                    continue
                if not call.initial.observations:
                    call.reasons.append("initial_window_unobserved")
                    continue
                call.chase = _Order(
                    seq, "CHASE", _ns(stamp),
                    min(_ns(schedule.chase_ts + timedelta(minutes=1)), _ns(schedule.exit_ts)),
                    min(qty, call.target), limit,
                )
        elif kind == "conditional_exit_intent":
            key = _paper_text(row.get("call_id"))
            call = calls.get(key)
            if call is None or call.exit is not None:
                raise ValueError("conditional exit identity mismatch")
            if (
                row.get("status") != "PAPER_INTENT_ONLY"
                or row.get("conditional_on") != "confirmed_entry_fill"
                or _paper_timestamp(row.get("scheduled_exit_ts_utc")) != call.schedule.exit_ts
                or stamp < call.schedule.exit_ts
            ):
                raise ValueError("conditional exit scope mismatch")
            deadline = min(_ns(call.schedule.exit_ts + timedelta(seconds=60)), close_ns)
            if call.bought and _ns(stamp) < deadline:
                call.exit = _Order(seq, "EXIT", _ns(stamp), deadline, call.bought, None)
        elif kind == "market_quote":
            symbol = _paper_text(row.get("symbol"))
            event = _paper_int(row.get("ts_event_ns"))
            wall = _paper_int(row.get("recv_wall_ns"))
            mono = _paper_int(row.get("recv_mono_ns"))
            if (
                not 0 < event <= wall <= _ns(stamp) + 2000
                or wall - event > STALE_POLICY.quote_max_age_ms * 1_000_000
                or wall < last_wall or mono < last_mono
                or event <= watermarks.get(symbol, -1)
            ):
                raise ValueError("market observation clock or ordering conflict")
            last_wall, last_mono = wall, mono
            watermarks[symbol] = event
            bid, ask = _paper_decimal(row.get("bid")), _paper_decimal(row.get("ask"))
            if ask < bid:
                raise ValueError("crossed market observation")
            if row.get("bid_size") is None or row.get("ask_size") is None:
                blockers.add("displayed_depth_missing")
                halted = True
                continue
            bsz, asz = _paper_int(row.get("bid_size")), _paper_int(row.get("ask_size"))
            publisher = _paper_int(row.get("publisher_id"))
            instrument = _paper_int(row.get("instrument_id"))
            if publisher <= 0 or instrument <= 0 or max(bsz, asz) > 2**32 - 1:
                raise ValueError("market depth identity invalid")
            if halted or not _ns(hours[0]) <= event < close_ns:
                continue
            liquidity = _depth(books.get(symbol), event, bid, ask, bsz, asz,
                               publisher, instrument)
            books[symbol] = liquidity
            # One shared displayed-depth budget per symbol, consumed in journal admission order.
            candidates: list[tuple[str, _Call, _Order]] = []
            for key, call in calls.items():
                if call.ticker != symbol:
                    continue
                for order in (call.initial, call.chase, call.exit):
                    if (
                        order and order.filled < order.quantity and order.seq < seq
                        and order.submitted_ns < event <= wall < order.deadline_ns
                    ):
                        candidates.append((key, call, order))
            for key, call, order in sorted(candidates, key=lambda item: item[2].seq):
                order.observations += 1
                if order.role == "EXIT":
                    qty = min(order.quantity - order.filled, call.bought - call.sold,
                              liquidity.bid_remaining)
                    price = bid
                    if qty:
                        liquidity.bid_remaining -= qty
                        call.sold += qty
                        call.sell_cash += qty * price
                else:
                    assert order.limit is not None
                    threshold = order.limit
                    if order.role == "INITIAL":
                        threshold *= 1 - Decimal(rules.trade_through_bps) / 10_000
                    qty = min(order.quantity - order.filled, liquidity.ask_remaining,
                              call.target - call.bought) if ask <= threshold else 0
                    price = order.limit  # deliberately pessimistic, not price improvement
                    if qty:
                        liquidity.ask_remaining -= qty
                        call.bought += qty
                        call.buy_cash += qty * price
                if qty:
                    order.filled += qty
                    fills.append(ModeledFill(
                        key, symbol, order.role, "SELL" if order.role == "EXIT" else "BUY",
                        qty, price, order.seq, seq, event,
                    ))

    end = rows[-1]
    if (
        end.get("kind") != "session_end" or end.get("market_records") != market_count
        or end.get("latch_armed") is not True or end.get("feed_disconnects") != 0
        or end.get("ingress_overflow") is not False or end.get("pending_ingress") != 0
        or end.get("ingress_callback_failures") != 0
        or end.get("safety_integrity_valid") is not True
        or start.get("signal_ingress_enabled") is not True
        or end.get("feed_first_usable_recv_wall_ns") is None
        or end.get("ingress_scope_ready_ts_utc") is None
        or end.get("ingress_first_disconnect_ts_utc") is not None
        or start.get("safety_integrity_valid") is not True
        or start.get("safety_event_count") != end.get("safety_event_count")
        or last_record is None or last_record < hours[1]
    ):
        blockers.add("incomplete_capture_lifecycle")
    if end.get("kind") == "session_end" and (
        end.get("feed_first_usable_recv_wall_ns") is not None
        and end.get("ingress_scope_ready_ts_utc") is not None
    ) and (
        _paper_int(end["feed_first_usable_recv_wall_ns"]) > _ns(hours[0])
        or _paper_timestamp(end["ingress_scope_ready_ts_utc"]) > hours[0]
    ):
        blockers.add("late_market_or_ingress_readiness")
    results: list[ModeledCall] = []
    inventory: dict[str, int] = {}
    for key, call in sorted(calls.items()):
        remaining = call.bought - call.sold
        if remaining:
            inventory[call.ticker] = inventory.get(call.ticker, 0) + remaining
        if not call.initial.observations:
            call.reasons.append("initial_window_unobserved")
        if call.chase is not None and not call.chase.observations:
            call.reasons.append("chase_window_unobserved")
        if remaining:
            call.reasons.append("unresolved_modeled_inventory")
        if not call.bought:
            state = "NO_MODELED_FILL" if not call.reasons else "UNRESOLVED"
        else:
            state = "MODELED_FLAT" if not remaining else "OPEN_MODELED"
        gross = call.sell_cash - call.buy_cash if call.bought and not remaining else None
        cost = call.buy_cash * rules.cost_bps_round_trip / 10_000 if gross is not None else None
        net = gross - cost if gross is not None and cost is not None else None
        blockers.update(call.reasons)
        results.append(ModeledCall(
            key, call.ticker, call.target, call.bought, call.sold, remaining,
            call.buy_cash, call.sell_cash, gross, cost, net, call.initial.observations,
            call.chase.observations if call.chase else 0,
            call.exit.observations if call.exit else 0, state,
        ))
    if not fills:
        blockers.add("no_modeled_fills")
    # Raw per-call estimates remain inspectable, but incomplete aggregate economics are withheld.
    aggregate = None if blockers else sum(
        (call.modeled_net_round_trip_usd for call in results
         if call.modeled_net_round_trip_usd is not None), Decimal(0),
    )
    identity = _paper_text(receipt.get("sha256"))
    kind = "RECOVERED_PREFIX" if recovered else "SEALED_LOCAL_CAPTURE"
    material = {
        "model": MODEL, "journal_sha256": identity, "input_kind": kind,
        "fills": fills, "calls": results, "blockers": sorted(blockers), "aggregate": aggregate,
    }
    return EquityPaperReport(
        identity, kind, MODEL, "XNAS.ITCH", "mbp-1", tuple(fills), tuple(results),
        market_count, tuple(sorted(inventory.items())), aggregate,
        tuple(sorted(blockers)), stable_hash(material),
    )

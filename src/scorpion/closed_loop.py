"""Closed-loop coverage: the one number, and the input behind every alert that misses it.

Every layer of this stack publishes a proof. The parser has a corpus. The bars cover 3 of 107
contracts. The pipeline replays 91 of 93 alerts. TCA measures 2 of 18 trades. Each number is
true and each is about a different denominator, so no one can answer the only question that
matters for the whole system:

    of the 93 alerts on record, how many can the stack take END TO END?

This module answers it as a chain of four segments, and refuses to round up:

    parse     the reference parser reads the reconstructed post unambiguously, and the
              contract it names is the contract the corpus recorded
    price     real bars exist for that contract on that day, and the entry sits inside their
              coverage (the `coverage.py` refusal set, reused rather than re-invented)
    execute   the venue acknowledged BOTH sides of the cycle - an entry ack and an exit ack -
              read from the live lane's own fill/execution log
    replay    the same alert replayed through the pipeline was taken and the applied log
              reproduced the book byte for byte

An alert counts only when all four hold. Today that is **0 of 93**, and the report says why:
segment 3 has no venue records until the live lane has traded. That is not a pessimistic
number, it is the honest one - and the direction document organises the whole fleet around it.

The refusal taxonomy is the product
-----------------------------------
Each alert stops at the FIRST segment it cannot pass, with a stable reason code. A report that
says "not covered" without naming the missing input teaches the next reader to guess, and
guessing is how a data gap becomes a strategy conclusion:

    unparsed:<kind>          the reference parser did not see an ENTRY
    parse_contract_mismatch  it saw an ENTRY for a different contract than the record
    no_bars / multi_...      the `coverage.py` reasons, unchanged
    no_entry_ack             the venue never acknowledged the buy
    no_exit_ack              the entry was confirmed but the cycle was never closed
    venue_record_malformed   an ack with no usable quantity or price
    no_replay                the corpus replay was not run (so segment 4 is unassessed)
    replay_unavailable       the replay has no row for this alert
    replay_refused           the replay's book refused the alert, with its reason
    replay_divergence        the applied log did not reproduce the book

Two refusals are deliberate. A missing venue log is `no_entry_ack`, never "execute skipped":
an unmeasured segment is a missing input, not a pass. And an unrun replay is `no_replay`, never
"replay assumed clean": the number is only as strong as its weakest assessed segment.

The live lane is consumed as an ARTIFACT, never imported
--------------------------------------------------------
`load_venue_records` reads the JSONL the live lane emits - both its fill log (`event` rows) and
its execution log (`t` rows) - and normalises it into `VenueRecord`. It does not import
`scorpion_mff`, does not share a module with it, and does not require the live lane to change a
format for this report. That is the seam the direction document specifies: the control plane
studies the shapes the live lane emits.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import CHANNELS
from .coverage import TradeCoverage, classify, contract_key_for
from .domain import EventKind, RawDiscordMessage
from .sessions import et_date

SAME_DAY_TOKENS = frozenset({"0dte", "today", "same-day", "same_day"})
AUTHOR = "signal-author"


class ClosedLoopError(ValueError):
    """A venue artifact that cannot be read as evidence. Never repaired silently."""


class Stage(StrEnum):
    """The four segments, in the order they are assessed."""

    PARSE = "parse"
    PRICE = "price"
    EXECUTE = "execute"
    REPLAY = "replay"


STAGES: tuple[Stage, ...] = (Stage.PARSE, Stage.PRICE, Stage.EXECUTE, Stage.REPLAY)


# ── the live lane's artifact ─────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class VenueRecord:
    """One confirmed side of a contract-day, normalised from the live lane's log."""

    contract_key: str
    action: str                    # "buy" | "sell"
    qty: int
    price: float
    ts: float
    order_key: str = ""
    source: str = ""

    @property
    def day(self) -> date:
        return et_date(datetime.fromtimestamp(self.ts, UTC))

    @property
    def parts(self) -> tuple[str, str, Decimal, date | None]:
        """(ticker, side, strike, expiry) with a same-day token resolved to the ET date."""
        ticker, side, strike, expiry = _split_contract(self.contract_key)
        expiry_day = _expiry_day(expiry, self.day)
        return ticker, side, strike, expiry_day

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_key": self.contract_key, "action": self.action, "qty": self.qty,
            "price": self.price, "ts": self.ts, "order_key": self.order_key,
            "source": self.source,
        }


def _split_contract(key: str) -> tuple[str, str, Decimal, str]:
    parts = (key or "").split("|")
    if len(parts) != 4:
        raise ClosedLoopError(f"{key!r} is not a contract key (TICKER|SIDE|STRIKE|EXP)")
    ticker, side, strike, expiry = parts
    try:
        return ticker.upper(), side.upper(), Decimal(strike), expiry
    except InvalidOperation as exc:
        raise ClosedLoopError(f"{key!r} has an unreadable strike: {exc}") from None


def _expiry_day(expiry: str, fallback: date) -> date | None:
    token = (expiry or "").strip().lower()
    if token in SAME_DAY_TOKENS or token in {"", "?"}:
        return fallback
    try:
        return date.fromisoformat(token)
    except ValueError:
        return None


def _float(value: object, *, what: str) -> float:
    try:
        number = float(value)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ClosedLoopError(f"{what} is not a number: {value!r}") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ClosedLoopError(f"{what} is not finite: {value!r}")
    return number


def _action_from_note(note: str, key: str) -> str:
    """The live lane's key carries the side: `...:buy:<ms>` / `...:close:<ms>`."""
    for marker in (":buy:", ":add:"):
        if marker in (note or "") or marker in (key or ""):
            return "buy"
    return "sell"


def load_venue_records(path: str | Path) -> list[VenueRecord]:
    """Read the live lane's confirmed answers, in either of the two shapes it emits.

    * a **fill log** - `{"event": "ack", "contract_key": ..., "qty": ..., "price": ..., "ts": ...}`
    * an **execution log** - `{"t": "intent"|"ack", "cid": ..., ...}`, where an ack names its
      order by `cid` and the intent row is the only place the contract key appears

    Only the venue's *answers* become records: a `buy`/`sell` decision row and an `intent` row
    are plans, and counting a plan as an execution is how an offline rehearsal becomes a
    fabricated closed loop. Anything that is neither shape is refused with its line number - a
    file that cannot be read is not an empty book.
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ClosedLoopError(f"cannot read venue log {target}: {exc}") from None
    rows: list[tuple[int, Mapping[str, Any]]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ClosedLoopError(f"{target}:{number} is not JSON: {exc}") from None
        if not isinstance(payload, Mapping):
            raise ClosedLoopError(f"{target}:{number} is not an object")
        rows.append((number, payload))
    if not rows:
        return []

    if any("event" in payload for _, payload in rows):
        return _from_fill_log(rows, target)
    if any("t" in payload for _, payload in rows):
        return _from_execution_log(rows, target)
    raise ClosedLoopError(
        f"{target} is neither a fill log (`event` rows) nor an execution log (`t` rows): "
        f"saw keys {sorted(rows[0][1])[:6]}")


def _from_fill_log(rows: Sequence[tuple[int, Mapping[str, Any]]],
                   target: Path) -> list[VenueRecord]:
    out: list[VenueRecord] = []
    for number, payload in rows:
        if payload.get("event") != "ack":
            continue
        contract = str(payload.get("contract_key", ""))
        if not contract:
            raise ClosedLoopError(f"{target}:{number} is an ack with no contract_key")
        qty = int(payload.get("qty", 0))
        price = _float(payload.get("price"), what=f"{target}:{number} price")
        ts = _float(payload.get("ts"), what=f"{target}:{number} ts")
        note = str(payload.get("note", ""))
        if qty <= 0 or price <= 0:
            out.append(VenueRecord(contract_key=contract,
                                   action=_action_from_note(note, contract),
                                   qty=max(qty, 0), price=max(price, 0.0), ts=ts,
                                   order_key=note, source="malformed"))
            continue
        out.append(VenueRecord(contract_key=contract,
                               action=_action_from_note(note, contract), qty=qty,
                               price=price, ts=ts, order_key=note, source="fill"))
    return out


def _from_execution_log(rows: Sequence[tuple[int, Mapping[str, Any]]],
                        target: Path) -> list[VenueRecord]:
    intents: dict[str, tuple[str, str, float]] = {}
    acks: dict[str, VenueRecord] = {}
    for number, payload in rows:
        kind = payload.get("t")
        cid = str(payload.get("cid", ""))
        if kind == "intent":
            contract = str(payload.get("contract_key", ""))
            if not contract:
                raise ClosedLoopError(f"{target}:{number} is an intent with no contract")
            if cid in intents:
                continue
            intents[cid] = (contract, str(payload.get("action", "buy")),
                            _float(payload.get("limit", 0.0), what=f"{target}:{number} limit"))
        elif kind == "ack":
            if cid not in intents:
                # The ack arrived for an order this log never recorded. That is an operator
                # area, not coverage evidence: adopting it would invent an intent.
                continue
            contract, action, limit = intents[cid]
            qty = int(payload.get("qty", 0))
            price = _float(payload.get("price", limit), what=f"{target}:{number} price")
            ts = _float(payload.get("ts"), what=f"{target}:{number} ts")
            # Cumulative acks: the last row for a cid is the confirmed state.
            acks[cid] = VenueRecord(
                contract_key=contract, action=action, qty=max(qty, 0), price=max(price, 0.0),
                ts=ts, order_key=cid,
                source="execution" if qty > 0 and price > 0 else "malformed")
    return list(acks.values())


# ── the per-alert assessment ─────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class AlertCoverage:
    identity: str
    channel: str
    contract_key: str
    reached: tuple[Stage, ...]
    missing: Stage | None
    reason: str
    detail: str = ""

    @property
    def closed(self) -> bool:
        """All four segments passed. Reaching three is not a closed loop."""
        return self.missing is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity, "channel": self.channel,
            "contract_key": self.contract_key,
            "reached": [stage.value for stage in self.reached],
            "missing": self.missing.value if self.missing is not None else None,
            "reason": self.reason, "detail": self.detail,
            "closed": self.closed,
        }


def _message(trade: Any) -> RawDiscordMessage:
    """The post the corpus implies, in the channel it came from.

    `entry_text` is the same reconstruction `corpus_replay` drives the pipeline with: a coverage
    number that parses a nicer sentence than the replay would is measuring a different system.
    """
    from .corpus_replay import entry_text

    channel_id = CHANNELS.get(str(trade.channel), CHANNELS["etf-options"])
    return RawDiscordMessage(
        message_id=trade.identity,
        guild_id="912747256736800838",
        channel_id=channel_id,
        author_id=AUTHOR,
        content=entry_text(trade),
        source_ts_utc=trade.ts_utc.astimezone(UTC),
        received_ts_utc=trade.ts_utc.astimezone(UTC),
    )


def _parse_stage(trade: Any) -> tuple[str, str]:
    """(reason, detail) when the reference parser cannot read this alert, else ("", "")."""
    from .parser import parse_message

    try:
        event = parse_message(_message(trade))
    except Exception as exc:             # noqa: BLE001 - a parse failure is data, not a crash
        return f"unparsed:{type(exc).__name__}", str(exc)[:120]
    if event.kind is not EventKind.ENTRY:
        return f"unparsed:{event.kind.value.lower()}", event.reason or "not an entry"
    # The parser identifies a contract by its EXPIRY, while `coverage` keys a bar set by the
    # trade DAY. Comparing the parser's key with the day-keyed one would report every dated
    # contract (54 of 107) as a mismatch - a measurement bug masquerading as a parse defect.
    # Compare by VALUE (a Decimal strike), not by string: the ledger writes `227.50` and the
    # parser writes `227.5`, and those are the same contract.
    expiry = _trade_expiry(trade)
    expected = (str(trade.ticker).upper(), str(trade.side).upper(),
                Decimal(str(trade.strike)), expiry or trade.day)
    try:
        ticker, side, strike, token = _split_contract(event.contract_key or "")
    except ClosedLoopError:
        return ("parse_contract_mismatch",
                f"parsed {event.contract_key or '<none>'} for {expected[0]}|{expected[1]}")
    parsed = (ticker, side, strike, _expiry_day(token, trade.day))
    if parsed[3] is None or parsed != expected:
        return ("parse_contract_mismatch",
                f"parsed {event.contract_key or '<none>'} for "
                f"{expected[0]}|{expected[1]}|{expected[2]}|{expected[3]}")
    return "", ""


def _price_stage(trade: Any, barset: Any) -> tuple[str, str]:
    """(reason, detail) from `coverage.classify`, reused so the reasons cannot diverge."""
    row: TradeCoverage = classify(trade, barset)
    if row.measurable:
        return "", f"{row.bars} bar(s)"
    return row.reason, row.detail


def _trade_expiry(trade: Any) -> date | None:
    kind = str(getattr(trade, "expiry_kind", "")).strip().lower()
    if kind in SAME_DAY_TOKENS:
        day: date = trade.day
        return day
    try:
        return date.fromisoformat(kind)
    except ValueError:
        return None


def _matching_records(trade: Any, venue: Sequence[VenueRecord]) -> list[VenueRecord]:
    ticker = str(trade.ticker).upper()
    side = str(trade.side).upper()
    strike = Decimal(str(trade.strike))
    expiry = _trade_expiry(trade)
    out: list[VenueRecord] = []
    for record in venue:
        try:
            rec_ticker, rec_side, rec_strike, rec_expiry = record.parts
        except ClosedLoopError:
            continue
        if (rec_ticker, rec_side, rec_strike) != (ticker, side, strike):
            continue
        # The contract has to be the SAME expiry: a buy of the same strike from a previous
        # session is not an acknowledgement of today's alert.
        if expiry is not None and rec_expiry is not None and rec_expiry != expiry:
            continue
        if record.day != trade.day:
            continue
        out.append(record)
    return out


def _execute_stage(trade: Any, venue: Sequence[VenueRecord]) -> tuple[str, str]:
    records = _matching_records(trade, venue)
    usable = [record for record in records if record.source != "malformed"]
    if not usable:
        return "no_entry_ack", "no venue acknowledgement for this contract-day"
    if not any(record.action == "buy" and record.qty > 0 for record in usable):
        return "no_entry_ack", f"{len(usable)} record(s), none for the entry"
    if not any(record.action == "sell" and record.qty > 0 for record in usable):
        return "no_exit_ack", "entry confirmed; the cycle was never closed at the venue"
    if any(record.source == "malformed" for record in records):
        return "venue_record_malformed", "an ack with no usable quantity or price"
    return "", f"{len(usable)} confirmed record(s)"


def _replay_stage(trade: Any, replay_result: Any) -> tuple[str, str]:
    """(reason, detail) from the corpus replay, or `no_replay` when it was not run."""
    if replay_result is None:
        return "no_replay", "segment 4 unassessed: pass --replay to run the corpus pipeline"
    rows = [row for row in replay_result.trades if row.identity == trade.identity]
    if not rows:
        return "replay_unavailable", "the replay has no row for this alert"
    row = rows[0]
    if row.status != "taken":
        return "replay_refused", f"{row.status}:{row.reason or 'unknown'}"
    verification = getattr(replay_result, "verification", {}) or {}
    if not verification.get("ok"):
        return "replay_divergence", "the applied log did not reproduce the book"
    return "", "taken; log reproduces the book"


def assess_alert(trade: Any, barset: Any, *, venue: Sequence[VenueRecord] = (),
                 replay_result: Any = None) -> AlertCoverage:
    """One alert through the four segments, stopping at the first it cannot pass."""
    identity = str(trade.identity)
    channel = str(trade.channel)
    contract_key = contract_key_for(trade)
    reached: list[Stage] = []
    checks: tuple[tuple[Stage, Callable[[], tuple[str, str]]], ...] = (
        (Stage.PARSE, lambda: _parse_stage(trade)),
        (Stage.PRICE, lambda: _price_stage(trade, barset)),
        (Stage.EXECUTE, lambda: _execute_stage(trade, venue)),
        (Stage.REPLAY, lambda: _replay_stage(trade, replay_result)),
    )
    for stage, check in checks:
        reason, detail = check()
        if reason:
            return AlertCoverage(identity=identity, channel=channel,
                                 contract_key=contract_key, reached=tuple(reached),
                                 missing=stage, reason=reason, detail=detail)
        reached.append(stage)
    # All four passed: a closed loop. Five minutes ago this was unreachable; the shape is
    # kept so the day the live lane trades, the number becomes non-zero without a rewrite.
    return AlertCoverage(identity=identity, channel=channel, contract_key=contract_key,
                         reached=tuple(reached), missing=None, reason="", detail="")


@dataclass
class ClosedLoopReport:
    """Every alert, its deepest segment, and the missing input that stopped it."""

    alerts: list[AlertCoverage] = field(default_factory=list)
    venue_records: int = 0
    venue_source: str = ""

    @property
    def total(self) -> int:
        return len(self.alerts)

    @property
    def closed(self) -> list[AlertCoverage]:
        return [row for row in self.alerts if row.closed]

    @property
    def coverage_pct(self) -> float:
        return round(100.0 * len(self.closed) / self.total, 1) if self.total else 0.0

    def reached(self, stage: Stage) -> int:
        """How many alerts got past this segment (i.e. include it in `reached`)."""
        return sum(1 for row in self.alerts if stage in row.reached)

    def blocking(self) -> dict[str, int]:
        """The first segment every unclosed alert failed to pass, by stable reason code."""
        counts: dict[str, int] = {}
        for row in self.alerts:
            if row.missing is None:
                continue
            key = f"{row.missing.value}:{row.reason}"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    def by_channel(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in self.alerts:
            entry = out.setdefault(row.channel, {"total": 0, "closed": 0, "missing": {}})
            entry["total"] += 1
            if row.closed:
                entry["closed"] += 1
            else:
                assert row.missing is not None
                reason = f"{row.missing.value}:{row.reason}"
                entry["missing"][reason] = entry["missing"].get(reason, 0) + 1
        for entry in out.values():
            entry["pct"] = (round(100.0 * entry["closed"] / entry["total"], 1)
                            if entry["total"] else 0.0)
        return dict(sorted(out.items()))

    def unclosed(self, reason: str | None = None) -> list[AlertCoverage]:
        return [row for row in self.alerts
                if not row.closed and (reason is None or row.reason == reason)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "alerts": self.total,
            "closed": len(self.closed),
            "coverage_pct": self.coverage_pct,
            "stages": {stage.value: self.reached(stage) for stage in STAGES},
            "blocking": self.blocking(),
            "by_channel": self.by_channel(),
            "venue": {"records": self.venue_records, "source": self.venue_source},
            "unclosed": [row.to_dict() for row in self.alerts if not row.closed],
        }

    def to_text(self) -> str:
        order = "  ".join(f"{stage.value} {self.reached(stage)}" for stage in STAGES)
        lines = [
            f"closed-loop  {len(self.closed)} / {self.total} "
            f"({self.coverage_pct:.1f}%)   {order}",
        ]
        if not self.venue_records:
            lines.append(
                "  venue       no confirmed records on record - segment 3 is unmeasured, "
                "not passed (pass --venue live/fills.jsonl)")
        else:
            lines.append(f"  venue       {self.venue_records} confirmed record(s) "
                         f"from {self.venue_source}")
        for key, count in self.blocking().items():
            lines.append(f"  blocking    {count:>3}  {key}")
        for channel, entry in self.by_channel().items():
            missing = ", ".join(f"{k} {v}" for k, v in entry["missing"].items()) or "none"
            lines.append(f"    {channel:26s} {entry['closed']:3d}/{entry['total']:<3d} "
                         f"{entry['pct']:5.1f}%  {missing}")
        return "\n".join(lines)


def assess(ledger: Any, barsets: Mapping[str, Any], *,
           venue: Sequence[VenueRecord] = (),
           replay_result: Any = None,
           venue_source: str = "") -> ClosedLoopReport:
    """Assess every trade in the ledger. `barsets` is keyed by `coverage.contract_key_for`."""
    report = ClosedLoopReport(venue_records=len(venue), venue_source=venue_source)
    for trade in ledger.trades:
        report.alerts.append(assess_alert(
            trade, barsets.get(contract_key_for(trade)), venue=venue,
            replay_result=replay_result))
    return report


def unclosed_detail(report: ClosedLoopReport, *, limit: int = 20) -> Iterable[str]:
    """One line per unclosed alert, for the operator who wants the names, not the counts."""
    for row in report.alerts:
        if row.closed or row.missing is None:
            continue
        yield (f"{row.identity:44s} {row.missing.value:<8s} {row.reason:<24s} "
               f"{row.detail[:80]}")
        limit -= 1
        if limit <= 0:
            return

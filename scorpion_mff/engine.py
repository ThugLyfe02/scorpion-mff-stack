"""The state machine: Signals + live quotes -> OrderIntents.

This is the piece the 9/8 failure asked for: deterministic, event-driven, and
off the agent's chat queue. Every decision - taken, skipped or rejected - lands
in the fill log with a reason, because the failure mode that cost ~$295-315 was
SILENCE, not a wrong trade.

Rules implemented (live/RULES.lock.md):
  * first trade of the day = 1 contract pipe test
  * tier clip by channel, add clip on an added/avg post, ONCE per position
  * max 2 concurrent positions
  * limit = min(ask, his_bid*1.15); skip if ask is > +25% off his bid
  * exit on close/trim/%/runners -> trim to 1 contract + move stop to breakeven
  * never chase a closed contract
  * STOP/stand-down halts new buys (exits stay allowed - you must be able to
    leave a position you are in)
  * a contract that reaches its close cutoff is flattened at a live bid, or paged
    to a human - never left to become an assignment (scorpion_mff/expiry.py)

The DAY is the ET trading date, not the UTC date (scorpion_mff/calendar.py). This is a
live-risk boundary, not a cosmetic one: a UTC label rolls the daily book at 20:00 ET and
closes a same-day contract on the previous evening's date at 01:30 UTC - the exact seam a
0DTE becomes an overnight hold.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .broker import QuoteProvider
from .calendar import CalendarError, et_date
from .execution import spread_ok, spread_ratio
from .expiry import (
    DEFAULT_CUTOFF_MINUTES,
    SAME_DAY_TOKENS,
    ExpiryVerdict,
    assignment_shares,
    contract_terms,
    is_live_bid,
    page_text,
    sweep,
)
from .exposure import ExposureLimits
from .exposure import guard as exposure_guard
from .fills import FillLog
from .governor import Governor, GovernorState, apply_contracts
from .pricing import far_off_bid, marketable_limit, pct_off_bid
from .reconcile import ReconcileReport, VenuePosition, reconcile
from .risk import RiskBudget
from .sizing import (
    MAX_CONCURRENT,
    PIPE_TEST_CONTRACTS,
    Tier,
    clip_budget,
    contracts_for,
    tier_for_channel,
)
from .stops import breakeven_exit
from .types import AlertKind, OrderIntent, Position, Quote, Signal


def _env_float(name: str) -> float | None:
    """Read an optional float from the environment; None when unset/unparsable."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _iso_date(value: str) -> str | None:
    """'2026-09-11' -> the same ISO string (validated); anything else -> None."""
    try:
        return time.strftime("%Y-%m-%d", time.strptime(value.strip(), "%Y-%m-%d"))
    except (ValueError, TypeError):
        return None


def _bid_value(value: float | Quote | None) -> float | None:
    """A quote map may carry a bare bid or a whole Quote; both mean the same here."""
    if value is None:
        return None
    if isinstance(value, Quote):
        return value.bid
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spread_pct(quote: Quote) -> float:
    """The bid/ask spread as a percentage of the bid, for the audit reason."""
    ratio = spread_ratio(quote)
    return 0.0 if ratio is None else ratio * 100.0


def _exposure(assignment: int | None) -> str:
    """Signed shares when they are knowable, the word 'unknown' when they are not.

    A missing underlying price is *not* an out-of-the-money contract. Printing 0 here
    would understate overnight risk on exactly the line a human reads at 15:45.
    """
    return "unknown" if assignment is None else f"{assignment:+d}"


@dataclass
class EngineConfig:
    max_concurrent: int = MAX_CONCURRENT
    trim_target: int = 1
    add_allowed_per_position: int = 1
    require_quote: bool = True          # fail closed: never guess a price
    pipe_test: bool = True              # 1 contract on the day's first trade
    enforce_breakeven: bool = False     # opt-in: flatten at the armed BE stop on a quote
    max_quote_age_s: float | None = None  # refuse a quote older than this (None = off)
    # Opt-in, and OFF by default so the historical replay and the corpus backtest keep
    # producing the same decisions they always did (the expiry guard would have
    # force-closed positions the corpus never exited). `cli arm` turns it on.
    enforce_expiry: bool = False
    expiry_cutoff_minutes: int = DEFAULT_CUTOFF_MINUTES   # from THIS session's close
    # Opt-in spread gate: refuse an entry whose bid/ask spread exceeds this ratio.
    # None = OFF (default, replay unchanged); a float arms it. Read from the
    # environment via MFF_MAX_SPREAD_RATIO so the live path can arm it without a
    # code change.
    max_spread_ratio: float | None = field(
        default_factory=lambda: _env_float("MFF_MAX_SPREAD_RATIO"))


@dataclass
class EngineStats:
    trades_today: int = 0
    realized_usd: float = 0.0
    skipped: int = 0
    rejected: int = 0
    intents: int = 0
    expired: int = 0                    # slots released on an unmarked expiry
    expiry_pages: int = 0               # HALT_AND_PAGE alerts raised by the guard


@dataclass
class Engine:
    """Deterministic. Same input sequence -> same output sequence, always."""

    config: EngineConfig = field(default_factory=EngineConfig)
    fills: FillLog = field(default_factory=FillLog)
    clock: Callable[[], float] = time.time
    blocked: Callable[[], bool] = lambda: False      # kill switch hook
    quotes: QuoteProvider | None = None              # fail closed: see _quote_for
    governor: Governor | None = None                 # reduce-only, consulted after sizing
    risk_budget: RiskBudget | None = None            # pre-trade worst-case loss; off until set
    exposure_limits: ExposureLimits | None = None    # correlated-book cap; off until set
    risk_state: GovernorState = field(default_factory=GovernorState)
    risk_state_path: str | os.PathLike[str] | None = None  # persist after each settle
    stats: EngineStats = field(default_factory=EngineStats)

    positions: dict[str, Position] = field(default_factory=dict)
    closed: set[str] = field(default_factory=set)
    halted: bool = False
    halt_reason: str = ""              # why buys are halted; exits stay legal
    day: str = ""
    # One HALT_AND_PAGE per (contract, reason, ET day): a tick every fifteen seconds must
    # not turn a real page into a stream a human learns to ignore. Cleared on the roll.
    expiry_paged: set[str] = field(default_factory=set)
    calendar_warning: str = ""          # last unusable-clock fallback (never silent)
    # The drift signature that was already paged this session. A scheduled reconcile runs
    # every few minutes; repeating an alert for an unchanged disagreement trains a human
    # to ignore the page, which is the failure mode a page exists to avoid.
    reconcile_drift: str = ""

    # ── the book ─────────────────────────────────────────────────────────────
    @property
    def governor_state(self) -> GovernorState:
        """Live risk state, kept in step with realised P&L.

        Held on the engine so a governor is a pure function of it: no second source of
        truth about how much has been lost today, and nothing to drift. `arm` seeds it
        from live/governor.json so a restart cannot forget a drawdown.
        """
        return self.risk_state

    def _book_result(self, delta_usd: float, ts: float) -> None:
        """One place where a realised result moves the stats and the risk state."""
        self.stats.realized_usd += delta_usd
        self.governor_state.record_result(
            delta_usd, time.strftime("%Y-%m-%d", time.gmtime(ts)))
        if self.risk_state_path is not None:
            try:
                self.governor_state.save(self.risk_state_path)
            except OSError as exc:
                # A risk file that cannot be written must not stop an exit from
                # settling, but it must be visible in the log.
                self.fills.record(event="alert", contract_key="", qty=0, price=0.0,
                                  reason=f"risk_state_unwritable:{exc}", ts=ts)

    def reconcile_positions(self, venue: Iterable[VenuePosition],
                            now: float | None = None,
                            local: Iterable[Position] | None = None
                            ) -> ReconcileReport:
        """Compare the book with the venue, and halt NEW BUYS on drift.

        The comparator is pure (`reconcile.py`); this method is the live consequence.
        A clean snapshot writes one `reconcile` row and changes nothing. Any drift
        sets `halted` with reason `reconcile-drift` and writes an alert that `watch`
        already pages on. Exits stay legal: a desk that cannot reconcile must still be
        able to leave a position it is in, and trapping it would be the worse failure.

        `local` is the book to compare. Left unset it is the engine's positions - the
        decisions it has taken. The live path passes the **confirmed** book once an
        execution log is attached, because the engine deletes a position at sell-intent
        time: a sell that never reached the venue would look flat locally while the
        venue still holds it, which is precisely the drift that costs money.

        Two consequences of running this on a schedule rather than by hand:

        * **An unchanged drift is recorded once.** The first snapshot that disagrees
          pages; every later snapshot with the same signature writes a quiet
          `drift_unchanged` row, so a five-minute cadence is one page, not twelve.
        * **A clean snapshot does not un-halt.** Drift is a session decision with a
          human in it; `halted` is shared with STOP/stand-down, and clearing it from a
          reconciliation would clear a stand-down nobody lifted. Recovery is an
          operator act (`delete live/governor.json` after a review, then re-arm), and
          the `clean_after_drift` row is the evidence that the books agree again.
        """
        ts = self.clock() if now is None else now
        book = list(local) if local is not None else self.open_positions
        report = reconcile(book, venue)
        if report.clean:
            after_drift = bool(self.reconcile_drift)
            self.reconcile_drift = ""
            self.fills.record(event="reconcile", contract_key="", qty=0, price=0.0,
                              reason="clean_after_drift" if after_drift else "clean",
                              ts=ts,
                              note=f"local={report.local_positions} "
                                   f"venue={report.venue_positions}")
            return report
        if self.reconcile_drift == report.reason_text:
            self.fills.record(event="reconcile", contract_key="", qty=0, price=0.0,
                              reason="drift_unchanged", ts=ts, accepted=False,
                              note=f"already paged: {report.reason_text}")
            return report
        self.reconcile_drift = report.reason_text
        self.halted = True
        self.halt_reason = "reconcile-drift"
        self.fills.record(event="alert", contract_key="", qty=0, price=0.0,
                          reason=f"reconcile_drift:{report.reason_text}", ts=ts,
                          accepted=False,
                          note="new buys halted; exits remain legal")
        return report

    # ── book helpers ─────────────────────────────────────────────────────────
    def _quote_for(self, key: str, explicit: Quote | None, now: float) -> Quote | None:
        """The quote for THIS decision - refused when missing or stale.

        An explicit quote always wins (that is what the tests and the replay
        scripts pass). Otherwise the provider is asked - and any failure becomes
        None, so the engine fails closed. An exit post that does not restate the
        contract has no key of its own; it is priced off the RESOLVED position,
        which is why this is called after _resolve, not before.

        A quote older than `EngineConfig.max_quote_age_s` (when set) is refused
        with a named alert: a cached price after a feed stall is a guess wearing
        a quote's clothes, and this path fails closed rather than trade on it.
        """
        quote = explicit
        if quote is None:
            if self.quotes is None or not key:
                return None
            try:
                quote = self.quotes.quote(key)
            except Exception:            # QuoteError and friends: unpriceable != priced
                return None
        if quote is None:
            return None
        if self._quote_is_stale(quote, now):
            self.fills.record(event="alert", contract_key=key, qty=0, price=0.0,
                              reason=f"stale_quote:{now - quote.ts:.1f}s", ts=now,
                              accepted=False)
            return None
        return quote

    def _quote_is_stale(self, quote: Quote, now: float) -> bool:
        """True when `quote` is older than the configured ceiling.

        Only a quote in the PAST relative to `now` is stale. A quote timestamped
        ahead of the clock is clock skew, not staleness, and is out of scope here
        - the offline tests run a fixed clock while explicit quotes carry a live
        `time.time()` stamp, so a symmetric check would refuse every test quote.
        """
        max_age = self.config.max_quote_age_s
        return max_age is not None and (now - quote.ts) > max_age

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def _resolve(self, signal: Signal) -> Position | None:
        """Find the position a non-entry post refers to.

        Real exits often do NOT restate the contract ("Close remaining for 10%"),
        so the resolution ladder is:
          1. the full contract key, when the post carries one
          2. ticker (+side, when stated), when that is unambiguous
          3. the single open position in the SAME CHANNEL - but ONLY for a post that
             names nothing at all
        Anything ambiguous resolves to None: flattening the wrong contract is worse
        than doing nothing, and the skip is logged.

        Step 3 used to run even when the post named a contract we do not hold. The
        fuzzer found it: "PLTR CALLs closing at 7.76" flattened a held NVDA put,
        because NVDA was the only position in that channel. A post that names a
        ticker, a side, or both is a deliberate reference; if it matches nothing, the
        answer is "no position", not "some position".
        """
        key = signal.contract_key
        if key and key in self.positions:
            return self.positions[key]

        named_something = bool(signal.ticker or signal.side)
        if named_something:
            matches = [p for p in self.open_positions
                       if (not signal.ticker or p.ticker == signal.ticker)
                       and (signal.side is None or p.side is signal.side)]
            if len(matches) == 1:
                return matches[0]
            # Zero matches (a contract we do not hold) or several (ambiguous): never
            # fall through to the channel, because the channel is not what was named.
            return None

        if signal.channel:
            in_channel = [p for p in self.open_positions if p.channel == signal.channel]
            if len(in_channel) == 1:
                return in_channel[0]
        return None

    def trading_day(self, ts: float) -> str:
        """Public name for the ET trading-date label (see `_trading_day`).

        The execution book needs the same day label the engine rolls on, so a restart
        replays `live/execution.jsonl` onto the session it actually belongs to instead
        of inventing a second definition of "today" from the UTC clock.
        """
        return self._trading_day(ts)

    def _trading_day(self, ts: float) -> str:
        """The ET trading date of an instant - the label the expiry book turns on.

        The UTC date is not the trading date. At 01:30 UTC a 2026-09-11 contract is still
        Thursday evening in New York, so a UTC label closes it a day early and rolls the
        daily book at 20:00 ET instead of midnight - both invisible until the one session
        where they decide whether a 0DTE becomes an assignment.

        The conversion needs only the DST rule, never the holiday tables, so it labels any
        instant it can represent (epoch 1000 included). Failing closed belongs to the
        SESSION lookup, and `expiry.py` is where the guard asks for one. A clock the
        calendar cannot read at all falls back to the UTC label rather than raising: the
        day boundary is bookkeeping and must not crash the hot path. That fallback is NOT
        silent - it is kept on `calendar_warning` and, when the expiry guard is armed (the
        only consumer for which the label is load-bearing), logged once as an alert.
        """
        try:
            return et_date(datetime.fromtimestamp(ts, tz=UTC)).isoformat()
        except (CalendarError, OverflowError, OSError, ValueError) as exc:
            warning = f"calendar_unusable:{exc}"
            if self.config.enforce_expiry and warning != self.calendar_warning:
                self.fills.record(event="alert", contract_key="", qty=0, price=0.0,
                                  reason=warning, ts=ts, accepted=False,
                                  note="ET day label fell back to UTC")
            self.calendar_warning = warning
            try:
                return time.strftime("%Y-%m-%d", time.gmtime(ts))
            except (OverflowError, OSError, ValueError):
                return ""            # a clock this broken has no day to roll

    def _roll_day(self, ts: float, underlying: Mapping[str, float] | None = None) -> None:
        """Reset the daily bookkeeping when the ET trading date changes.

        Without this, "never chase a closed contract" would block a 0DTE key
        forever: the same `QQQ|CALL|719|0DTE` string recurs on any day the
        contract trades, and the day's first trade must be a pipe test again.
        The roll is logged so the trace shows where the boundary was.
        """
        day = self._trading_day(ts)
        if not self.day:
            self.day = day
            return
        if day == self.day:
            return
        self.day = day
        self.closed.clear()
        self.stats.trades_today = 0
        self.expiry_paged.clear()
        self._expire_carried(day, ts, underlying)
        self.fills.record(event="day_roll", contract_key="", qty=0, price=0.0,
                          reason=f"new_day:{day}", ts=ts)

    def _expire_carried(self, day: str, ts: float,
                        underlying: Mapping[str, float] | None = None) -> None:
        """Release positions whose contract has provably stopped existing - and say so.

        MAX_CONCURRENT is a LIVE-RISK limit, so a position that expired must not
        hold a slot. Without this sweep one missed exit silently disables the hone
        for the rest of the month: driving the historical corpus proves it - an
        alert with no recorded exit kept the book at 2/2 and every later entry was
        skipped (see BACKTEST.md). The release is deliberately conservative:

          * a same-day token in the key ("0DTE"/"ODTE"/"TODAY") -> expired at that
            session's close
          * an ISO expiry date earlier than the ET trading date -> expired
          * anything else ("Dec 5") -> NOT released; it raises an alert instead,
            because freeing a slot on an unparsed expiry is a guess.

        The position is marked at its own entry price with accepted=False: the
        contract's real settle is unknown, so it must not enter realised P&L.

        The release is never SILENT. Every path writes an `expiry_unresolved` alert
        (released) or `expiry_unparsed` alert (kept) that carries the projected
        assignment shares - or the word `unknown` when there is no underlying close to
        project from - plus the reason. A released slot is the one case where the book
        stops counting a real overnight risk, so it is exactly the case a human has to
        be able to reconstruct afterwards.
        """
        prices = underlying or {}
        for pos in list(self.positions.values()):
            if not pos.is_open:
                continue
            key = pos.contract_key
            expiry = key.rsplit("|", 1)[-1]
            iso = _iso_date(expiry)
            terms = contract_terms(key)
            ticker = terms.ticker if terms is not None else (pos.ticker or "")
            shares = assignment_shares(key, pos.contracts, prices.get(ticker))
            exposure = _exposure(shares)
            same_day = expiry.strip().upper() in SAME_DAY_TOKENS
            released = same_day or (iso is not None and iso < day)
            if released:
                why = "same_day" if same_day else "past_expiry"
                self.fills.record(event="expire", contract_key=key,
                                  qty=pos.contracts, price=pos.avg_price,
                                  reason=f"expired:{expiry}", ts=ts, accepted=False)
                self.stats.expired += 1
                self.fills.record(
                    event="alert", contract_key=key, qty=pos.contracts,
                    price=pos.avg_price, ts=ts, accepted=False,
                    reason=f"expiry_unresolved:{why}:{expiry}:assignment={exposure}",
                    note=f"slot released on ET date {day}; settle unknown, so this is "
                         f"not in realised P&L")
                pos.contracts = 0
            elif iso is None:
                self.fills.record(
                    event="alert", contract_key=key, qty=pos.contracts,
                    price=pos.avg_price, ts=ts, accepted=False,
                    reason=f"expiry_unparsed:{expiry}:assignment={exposure}",
                    note="slot NOT released: an unreadable expiry is not an expired one")

    def _ambiguous_exit(self, signal: Signal) -> bool:
        """An exit that names no contract while two positions sit in its channel."""
        if signal.contract_key or not signal.channel:
            return False
        return len([p for p in self.open_positions if p.channel == signal.channel]) > 1

    # ── the entry point ──────────────────────────────────────────────────────
    def on_signal(self, signal: Signal, quote: Quote | None = None,
                  now: float | None = None, latency_ms: int | None = None) -> list[OrderIntent]:
        ts = self.clock() if now is None else now
        self._roll_day(ts)
        if not signal.actionable:
            reason = signal.ignore_reason.value if signal.ignore_reason else "unknown"
            self._skip(signal, ts, f"ignored:{reason}", latency_ms)
            return []

        handler = {
            AlertKind.ENTRY: self._on_entry,
            AlertKind.ADD: self._on_add,
            AlertKind.TRIM: self._on_trim,
            AlertKind.CLOSE: self._on_close,
            AlertKind.STOP: self._on_stop,
        }[signal.kind]
        return handler(signal, quote, ts, latency_ms)

    # ── handlers ─────────────────────────────────────────────────────────────
    def _on_stop(self, signal: Signal, quote: Quote | None, ts: float,
                 latency_ms: int | None) -> list[OrderIntent]:
        self.halted = True
        self.halt_reason = "stand-down"
        self.fills.record(event="halt", contract_key="", qty=0, price=0.0,
                          reason="stand-down", ts=ts, latency_ms=latency_ms,
                          note=signal.raw[:120])
        return []

    def _on_entry(self, signal: Signal, quote: Quote | None, ts: float,
                  latency_ms: int | None) -> list[OrderIntent]:
        if self.halted:
            return self._reject(signal, ts, f"halted:{self.halt_reason or 'stand-down'}",
                                latency_ms)
        if self.blocked():
            return self._reject(signal, ts, "halted:kill-switch", latency_ms)
        key = signal.contract_key
        if not key:
            return self._skip(signal, ts, "unparseable_contract", latency_ms)
        if key in self.closed:
            return self._skip(signal, ts, "already_closed:never_chase", latency_ms)
        if key in self.positions:
            return self._skip(signal, ts, "already_open", latency_ms)
        if len(self.open_positions) >= self.config.max_concurrent:
            return self._skip(signal, ts, f"max_concurrent:{self.config.max_concurrent}",
                              latency_ms)
        if signal.his_bid is None:
            return self._reject(signal, ts, "no_his_bid", latency_ms)
        if signal.side is None or signal.strike is None:
            # AlertKind.ENTRY already implies both; this keeps the invariant
            # local and explicit rather than trusting the classifier.
            return self._skip(signal, ts, "unparseable_contract", latency_ms)
        quote = self._quote_for(key, quote, ts)
        if quote is None:
            return self._reject(signal, ts, "no_quote", latency_ms)
        if far_off_bid(signal.his_bid, quote.ask):
            return self._skip(signal, ts,
                              f"too_far_off_bid:{pct_off_bid(signal.his_bid, quote.ask):.1f}%",
                              latency_ms)
        if not spread_ok(quote, self.config.max_spread_ratio):
            return self._skip(signal, ts,
                              f"spread_too_wide:{_spread_pct(quote):.1f}%", latency_ms)

        first_of_day = self.stats.trades_today == 0
        tier = tier_for_channel(signal.channel,
                                is_first_of_day=first_of_day and self.config.pipe_test)
        limit = marketable_limit(signal.his_bid, quote.ask)
        qty = (PIPE_TEST_CONTRACTS if tier is Tier.PIPE
               else contracts_for(clip_budget(tier), limit))

        # The governor can only shrink the locked clip. It is consulted AFTER the
        # locked size exists, so "reduce-only" is structural rather than a promise:
        # apply_contracts() also clamps, in case the policy was hand-edited.
        if self.governor is not None:
            decision = self.governor(self.governor_state, signal, ts)
            qty = apply_contracts(qty, decision.factor)
            if decision.stand_down or qty <= 0:
                # A stand-down is a decision worth recording, never a silent drop.
                return self._skip(signal, ts, f"governor:{decision.reason_text}", latency_ms)

        # The risk budget prices the loss BEFORE it happens. The governor above reacts
        # to realised P&L, which cannot fire before the loss exists; this pass shrinks
        # (never grows) the locked clip so the day's worst case stays inside the limit.
        budget_note = ""
        if self.risk_budget is not None:
            admission = self.risk_budget.admit(
                self.open_positions, ticker=signal.ticker, qty=qty, limit=limit,
                realized_today_usd=self.governor_state.realized_today_usd)
            if not admission.accepted:
                return self._skip(signal, ts, f"risk_budget:{admission.reason_text}",
                                  latency_ms)
            if admission.capped:
                qty = admission.qty
                budget_note = f":budget={admission.reason_text}"

        # Concentration is the cap a position count cannot express. It is checked after
        # sizing because the premium cap needs the sized quantity and limit; a refusal is
        # a skip with a named reason, never a silent drop, and the book is unchanged.
        if self.exposure_limits is not None:
            verdict = exposure_guard(self.exposure_limits, self.open_positions, signal,
                                     incoming_qty=qty, incoming_price=limit)
            if not verdict.allowed:
                return self._skip(signal, ts, f"exposure:{verdict.reason}", latency_ms)

        position = Position(contract_key=key, ticker=signal.ticker, side=signal.side,
                            contracts=qty, avg_price=limit, opened_ts=ts,
                            channel=signal.channel)
        self.positions[key] = position
        self.stats.trades_today += 1
        self.stats.intents += 1
        intent = OrderIntent(
            action="buy", qty=qty, limit=limit, contract_key=key,
            reason=(f"entry:{tier.value}:his_bid={signal.his_bid:g}:ask={quote.ask:g}"
                    f"{budget_note}"),
            idempotency_key=f"{key}:buy:{self.stats.trades_today}",
        )
        self.fills.record(event="buy", contract_key=key, qty=qty, price=limit,
                          reason=intent.reason, ts=ts, latency_ms=latency_ms,
                          note=signal.channel)
        return [intent]

    def _on_add(self, signal: Signal, quote: Quote | None, ts: float,
                latency_ms: int | None) -> list[OrderIntent]:
        position = self._resolve(signal)
        key = signal.contract_key or (position.contract_key if position else "")
        if position is None:
            return self._skip(signal, ts, "add_without_position", latency_ms, key)
        # A STOP halts ENTRIES and ADDS. The original add path checked only the kill
        # switch, so "stand down" followed by "adding at 1.30" increased exposure on a
        # desk that had been told to stop. Exits remain legal: you must always be able
        # to leave a position you are in.
        if self.halted:
            return self._reject(signal, ts, f"halted:{self.halt_reason or 'stand-down'}",
                                latency_ms, key)
        if self.blocked():
            return self._reject(signal, ts, "halted:kill-switch", latency_ms, key)
        if position.adds >= self.config.add_allowed_per_position:
            return self._skip(signal, ts, "add_already_used", latency_ms, key)
        price = signal.price if signal.price is not None else signal.his_bid
        if price is None:
            return self._reject(signal, ts, "no_price_for_add", latency_ms, key)
        quote = self._quote_for(position.contract_key, quote, ts)
        if quote is None:
            return self._reject(signal, ts, "no_quote", latency_ms, key)
        limit = marketable_limit(price, quote.ask)
        tier = tier_for_channel(signal.channel or position.ticker, is_first_of_day=False)
        qty = contracts_for(clip_budget(tier, is_add=True), limit)

        # An add is a new buy, not a footnote to the first one. It runs the SAME
        # governor the entry ran; the old path skipped it, so a daily stop, a loss
        # ladder or a channel tilt could be undone by the next "adding" post.
        governor_note = ""
        if self.governor is not None:
            decision = self.governor(self.governor_state, signal, ts)
            qty = apply_contracts(qty, decision.factor)
            if decision.stand_down or qty <= 0:
                return self._skip(signal, ts, f"governor:{decision.reason_text}",
                                  latency_ms, key)
            if decision.factor < 1.0:
                governor_note = f":governor={decision.reason_text}"

        budget_note = ""
        if self.risk_budget is not None:
            admission = self.risk_budget.admit(
                self.open_positions, ticker=position.ticker, qty=qty, limit=limit,
                realized_today_usd=self.governor_state.realized_today_usd)
            if not admission.accepted:
                return self._skip(signal, ts, f"risk_budget:{admission.reason_text}",
                                  latency_ms, key)
            if admission.capped:
                qty = admission.qty
                budget_note = f":budget={admission.reason_text}"
        total = position.contracts + qty
        position.avg_price = (position.avg_price * position.contracts + limit * qty) / total
        position.contracts = total
        position.adds += 1
        self.stats.intents += 1
        intent = OrderIntent(action="buy", qty=qty, limit=limit, contract_key=key,
                             reason=(f"add:{tier.value}:"
                                     f"{position.adds}/{self.config.add_allowed_per_position}"
                                     f"{governor_note}{budget_note}"),
                             idempotency_key=f"{key}:add:{position.adds}")
        self.fills.record(event="buy", contract_key=key, qty=qty, price=limit,
                          reason=intent.reason, ts=ts, latency_ms=latency_ms, note="add")
        return [intent]

    def _on_trim(self, signal: Signal, quote: Quote | None, ts: float,
                 latency_ms: int | None) -> list[OrderIntent]:
        position = self._resolve(signal)
        key = signal.contract_key or (position.contract_key if position else "")
        if position is None:
            if self._ambiguous_exit(signal):
                return self._alert(signal, ts, "ambiguous_exit:named_no_contract", latency_ms)
            return self._skip(signal, ts, "trim_without_position", latency_ms, key)
        qty = position.contracts - self.config.trim_target
        if qty <= 0:
            # Nothing to scale out, but the instruction is still "protect it":
            # move the stop to breakeven before recording the no-op.
            position.breakeven_stop = position.avg_price
            return self._skip(signal, ts, "nothing_to_trim", latency_ms, key)
        quote = self._quote_for(position.contract_key, quote, ts)
        price = signal.price if signal.price is not None else (
            quote.bid if quote is not None else None)
        if price is None:
            return self._reject(signal, ts, "no_price_for_trim", latency_ms, key)
        self._book_result((price - position.avg_price) * qty * 100, ts)
        position.contracts = self.config.trim_target
        position.trimmed += 1
        position.breakeven_stop = position.avg_price
        self.stats.intents += 1
        pct = f"{signal.pct:g}%" if signal.pct is not None else "n/a"
        intent = OrderIntent(action="sell", qty=qty, limit=price, contract_key=key,
                             reason=f"trim_to_{self.config.trim_target}+BE:pct={pct}",
                             idempotency_key=f"{key}:trim:{position.trimmed}")
        self.fills.record(event="sell", contract_key=key, qty=qty, price=price,
                          reason=intent.reason, ts=ts, latency_ms=latency_ms,
                          note=f"stop->be@{position.avg_price:g}")
        return [intent]

    def _on_close(self, signal: Signal, quote: Quote | None, ts: float,
                  latency_ms: int | None) -> list[OrderIntent]:
        position = self._resolve(signal)
        key = signal.contract_key or (position.contract_key if position else "")
        if position is None:
            if self._ambiguous_exit(signal):
                return self._alert(signal, ts, "ambiguous_exit:named_no_contract", latency_ms)
            if signal.contract_key in self.closed:
                closed_note = "already_closed:never_chase"
            else:
                closed_note = "close_without_position"
            return self._skip(signal, ts, closed_note, latency_ms, key)
        qty = position.contracts
        quote = self._quote_for(position.contract_key, quote, ts)
        price = signal.price if signal.price is not None else (
            quote.bid if quote is not None else None)
        if price is None:
            return self._reject(signal, ts, "no_price_for_close", latency_ms, key)
        self._book_result((price - position.avg_price) * qty * 100, ts)
        position.contracts = 0
        self.closed.add(position.contract_key)
        del self.positions[position.contract_key]
        self.stats.intents += 1
        pct = f"{signal.pct:g}%" if signal.pct is not None else "n/a"
        intent = OrderIntent(action="sell", qty=qty, limit=price, contract_key=key,
                             reason=f"close:pct={pct}", idempotency_key=f"{key}:close")
        self.fills.record(event="sell", contract_key=key, qty=qty, price=price,
                          reason=intent.reason, ts=ts, latency_ms=latency_ms, note="flat")
        return [intent]

    # ── the breakeven stop, made live ────────────────────────────────────────
    def on_quote(self, contract_key: str, quote: Quote, now: float | None = None,
                 latency_ms: int | None = None) -> list[OrderIntent]:
        """Enforce the breakeven stop the trim armed, on a fresh quote.

        Off unless `EngineConfig.enforce_breakeven` is set. When on, a bid at or
        through a position's `breakeven_stop` flattens the whole remainder at the
        bid actually seen - never the stop price - so realised P&L stays honest
        when the market gaps through. This is the missing half of "trim to 1 + BE":
        the trim armed the stop; this fires it.
        """
        if not self.config.enforce_breakeven:
            return []
        ts = self.clock() if now is None else now
        if self._quote_is_stale(quote, ts):
            # A stale bid must not fire a stop: selling at a price that no longer
            # exists is the same lie as buying at one, and the fill log stays honest
            # only if the stop fires on a bid we could actually have hit.
            return []
        position = self.positions.get(contract_key)
        if position is None or not position.is_open:
            return []
        intent = breakeven_exit(position, quote.bid)
        if intent is None:
            return []
        self._book_result((intent.limit - position.avg_price) * intent.qty * 100, ts)
        position.contracts = 0
        self.closed.add(position.contract_key)
        del self.positions[position.contract_key]
        self.stats.intents += 1
        self.fills.record(event="sell", contract_key=intent.contract_key, qty=intent.qty,
                          price=intent.limit, reason=intent.reason, ts=ts,
                          latency_ms=latency_ms, note="breakeven_stop")
        return [intent]

    # ── the expiry guard, made live ──────────────────────────────────────────
    def expiry_intents(self, now: float | None = None,
                       quotes: Mapping[str, float | Quote] | None = None,
                       underlying: Mapping[str, float] | None = None,
                       cutoff_minutes: int | None = None) -> list[OrderIntent]:
        """What the book owes the close, decided on a clock instead of a post.

        `scorpion_mff.expiry` owns the decision (HOLD / FORCE_CLOSE / HALT_AND_PAGE) and
        this only acts on it, which is the point: the guard is not a second opinion about
        expiry, it is the same one, finally wired to the executor.

          * FORCE_CLOSE  -> sell the whole remainder at the bid actually seen, book the
                            realised result, and free the slot;
          * HALT_AND_PAGE -> record a page (deduped per ET day) and place NOTHING. An
                            unpriced exit is a guess wearing a price; leaving the
                            position on the book keeps the risk counted.

        Off unless `EngineConfig.enforce_expiry` is set, so a replay of a recorded tape
        and the 93-alert corpus backtest keep their original decisions byte for byte.

        `quotes` maps contract key -> bid (a bare float or a Quote). A missing key, a
        zero, a negative or a NaN is SILENCE, never a price and never an OTM. `underlying`
        maps ticker -> spot and only ever changes the page's evidence, never the action.
        """
        if not self.config.enforce_expiry:
            return []
        ts = self.clock() if now is None else now
        self._roll_day(ts, underlying)
        cutoff = (self.config.expiry_cutoff_minutes if cutoff_minutes is None
                  else cutoff_minutes)
        bids: dict[str, float] = {}
        for key, raw in (quotes or {}).items():
            bid = _bid_value(raw)
            if bid is not None and is_live_bid(bid):
                bids[key] = bid
        try:
            verdicts = sweep(ts, self.open_positions, bids, underlying or {}, cutoff)
        except CalendarError as exc:
            # Fail closed: with no calendar there is no cutoff to be right about, so
            # nothing is flattened and a human is told why.
            self._expiry_page("", f"expiry_calendar_unknown_year:{exc}", ts,
                              note="no session known: nothing was flattened")
            return []
        intents: list[OrderIntent] = []
        for verdict in verdicts:
            if verdict.must_flatten:
                intent = self._expiry_flatten(verdict, bids.get(verdict.contract_key), ts)
                if intent is not None:
                    intents.append(intent)
            else:
                self._expiry_page(verdict.contract_key, verdict.reason, ts,
                                  note=page_text(verdict),
                                  shares=verdict.assignment_shares)
        return intents

    def _expiry_flatten(self, verdict: ExpiryVerdict, bid: float | None,
                        ts: float) -> OrderIntent | None:
        """Sell the remainder at the bid the verdict was decided on.

        If the bid vanished between the verdict and the order, nothing is placed: the
        same silence that produced the FORCE_CLOSE cannot also produce its price.
        """
        limit = _bid_value(bid)
        if limit is None or not is_live_bid(limit):
            self._expiry_page(verdict.contract_key, "bid_vanished_before_exit", ts,
                              note=page_text(verdict),
                              shares=verdict.assignment_shares)
            return None
        position = self.positions.get(verdict.contract_key)
        if position is None or not position.is_open:
            return None
        qty = position.contracts
        self._book_result((limit - position.avg_price) * qty * 100, ts)
        position.contracts = 0
        self.closed.add(position.contract_key)
        del self.positions[position.contract_key]
        self.stats.intents += 1
        minutes = verdict.minutes_to_close
        reason = f"expiry:force_close:bid={limit:g}" + (
            f":{minutes:+.1f}m" if minutes is not None else "")
        intent = OrderIntent(action="sell", qty=qty, limit=limit,
                             contract_key=verdict.contract_key, reason=reason,
                             idempotency_key=f"{verdict.contract_key}:expiry")
        self.fills.record(event="sell", contract_key=verdict.contract_key, qty=qty,
                          price=limit, reason=reason, ts=ts,
                          note=f"expiry_cutoff_force_close:{verdict.reason}")
        return intent

    def _expiry_page(self, key: str, reason: str, ts: float, note: str = "",
                     shares: int | None = None) -> bool:
        """Record a HALT_AND_PAGE once per (contract, reason, ET day).

        Returns whether the page was written. Deduped because the guard runs on a tick:
        an alert every fifteen seconds is not a page, it is noise that trains a human to
        stop reading - and the one that matters is then the one nobody reads.
        """
        text = f"HALT_AND_PAGE:{reason}:assignment={_exposure(shares)}"
        token = f"{key}|{text}"
        if token in self.expiry_paged:
            return False
        self.expiry_paged.add(token)
        self.stats.expiry_pages += 1
        position = self.positions.get(key)
        self.fills.record(event="alert", contract_key=key,
                          qty=position.contracts if position is not None else 0,
                          price=position.avg_price if position is not None else 0.0,
                          reason=text, ts=ts, accepted=False, note=note[:240])
        return True

    # ── logging helpers ──────────────────────────────────────────────────────
    def _skip(self, signal: Signal, ts: float, reason: str, latency_ms: int | None,
              key: str = "") -> list[OrderIntent]:
        self.stats.skipped += 1
        self.fills.record(event="skip", contract_key=key or signal.contract_key, qty=0,
                          price=0.0, reason=reason, ts=ts, latency_ms=latency_ms,
                          accepted=False, note=signal.raw[:120])
        return []

    def _reject(self, signal: Signal, ts: float, reason: str, latency_ms: int | None,
                key: str = "") -> list[OrderIntent]:
        self.stats.rejected += 1
        self.fills.record(event="reject", contract_key=key or signal.contract_key, qty=0,
                          price=0.0, reason=reason, ts=ts, latency_ms=latency_ms,
                          accepted=False, note=signal.raw[:120])
        return []

    def _alert(self, signal: Signal, ts: float, reason: str,
               latency_ms: int | None) -> list[OrderIntent]:
        """Something a human must resolve. Logged as its own event so it is
        greppable rather than buried in the skip stream - and never guessed at."""
        self.stats.skipped += 1
        self.fills.record(event="alert", contract_key=signal.contract_key, qty=0,
                          price=0.0, reason=reason, ts=ts, latency_ms=latency_ms,
                          accepted=False, note=signal.raw[:120])
        return []

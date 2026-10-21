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
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .fills import FillLog
from .pricing import far_off_bid, marketable_limit, pct_off_bid
from .governor import GovernorState, apply_contracts
from .sizing import (
    MAX_CONCURRENT,
    PIPE_TEST_CONTRACTS,
    Tier,
    clip_budget,
    contracts_for,
    tier_for_channel,
)
from .types import AlertKind, OrderIntent, Position, Quote, Signal


def _iso_date(value: str) -> str | None:
    """'2026-09-11' -> the same ISO string (validated); anything else -> None."""
    try:
        return time.strftime("%Y-%m-%d", time.strptime(value.strip(), "%Y-%m-%d"))
    except (ValueError, TypeError):
        return None


@dataclass
class EngineConfig:
    max_concurrent: int = MAX_CONCURRENT
    trim_target: int = 1
    add_allowed_per_position: int = 1
    require_quote: bool = True          # fail closed: never guess a price
    pipe_test: bool = True              # 1 contract on the day's first trade


@dataclass
class EngineStats:
    trades_today: int = 0
    realized_usd: float = 0.0
    skipped: int = 0
    rejected: int = 0
    intents: int = 0
    expired: int = 0


@dataclass
class Engine:
    """Deterministic. Same input sequence -> same output sequence, always."""

    config: EngineConfig = field(default_factory=EngineConfig)
    fills: FillLog = field(default_factory=FillLog)
    clock: Callable[[], float] = time.time
    blocked: Callable[[], bool] = lambda: False      # kill switch hook
    quotes: Any = None                               # QuoteProvider; see _quote_for
    governor: Any = None                             # governor.Governor or None
    risk_state: GovernorState = field(default_factory=GovernorState)
    risk_state_path: Any = None                      # persist risk state after each settle
    stats: EngineStats = field(default_factory=EngineStats)

    positions: dict[str, Position] = field(default_factory=dict)
    closed: set[str] = field(default_factory=set)
    halted: bool = False
    day: str = ""

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

    # ── book helpers ─────────────────────────────────────────────────────────
    def _quote_for(self, key: str, explicit: Quote | None) -> Quote | None:
        """The quote for THIS decision.

        An explicit quote always wins (that is what the tests and the replay
        scripts pass). Otherwise the provider is asked - and any failure becomes
        None, so the engine fails closed. An exit post that does not restate the
        contract has no key of its own; it is priced off the RESOLVED position,
        which is why this is called after _resolve, not before.
        """
        if explicit is not None:
            return explicit
        if self.quotes is None or not key:
            return None
        try:
            return self.quotes.quote(key)
        except Exception:            # QuoteError and friends: unpriceable != priced
            return None

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def _resolve(self, signal: Signal) -> Position | None:
        """Find the position a non-entry post refers to.

        Real exits often do NOT restate the contract ("Close remaining for 10%"),
        so the resolution ladder is:
          1. the full contract key, when the post carries one
          2. ticker+side, when that is unambiguous
          3. the single open position in the SAME CHANNEL - he posts an exit
             where he posted the entry
        Anything ambiguous resolves to None: flattening the wrong contract is
        worse than doing nothing, and the skip is logged.
        """
        key = signal.contract_key
        if key and key in self.positions:
            return self.positions[key]
        if signal.ticker and signal.side:
            matches = [p for p in self.open_positions
                       if p.ticker == signal.ticker and p.side is signal.side]
            if len(matches) == 1:
                return matches[0]
        if signal.channel:
            in_channel = [p for p in self.open_positions if p.channel == signal.channel]
            if len(in_channel) == 1:
                return in_channel[0]
        return None

    def _roll_day(self, ts: float) -> None:
        """Reset the daily bookkeeping when the UTC day changes.

        Without this, "never chase a closed contract" would block a 0DTE key
        forever: the same `QQQ|CALL|719|0DTE` string recurs on any day the
        contract trades, and the day's first trade must be a pipe test again.
        The roll is logged so the trace shows where the boundary was.
        """
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        if not self.day:
            self.day = day
            return
        if day == self.day:
            return
        self.day = day
        self.closed.clear()
        self.stats.trades_today = 0
        self._expire_carried(day, ts)
        self.fills.record(event="day_roll", contract_key="", qty=0, price=0.0,
                          reason=f"new_day:{day}", ts=ts)

    def _expire_carried(self, day: str, ts: float) -> None:
        """Release positions whose contract has provably stopped existing.

        MAX_CONCURRENT is a LIVE-RISK limit, so a position that expired must not
        hold a slot. Without this sweep one missed exit silently disables the hone
        for the rest of the month: driving the historical corpus proves it - an
        alert with no recorded exit kept the book at 2/2 and every later entry was
        skipped (see BACKTEST.md). The release is deliberately conservative:

          * "0DTE" in the key  -> expired at that day's close
          * an ISO expiry date earlier than today -> expired
          * anything else ("Dec 5") -> NOT released; it raises an alert instead,
            because freeing a slot on an unparsed expiry is a guess.

        The position is marked at its own entry price with accepted=False: the
        contract's real settle is unknown, so it must not enter realised P&L.
        """
        for pos in list(self.positions.values()):
            if not pos.is_open:
                continue
            expiry = pos.contract_key.rsplit("|", 1)[-1]
            iso = _iso_date(expiry)
            if expiry.upper() == "0DTE" or (iso is not None and iso < day):
                self.fills.record(event="expire", contract_key=pos.contract_key,
                                  qty=pos.contracts, price=pos.avg_price,
                                  reason=f"expired:{expiry}", ts=ts, accepted=False)
                self.stats.expired += 1
                pos.contracts = 0
            elif iso is None:
                self.fills.record(event="alert", contract_key=pos.contract_key,
                                  qty=pos.contracts, price=pos.avg_price,
                                  reason=f"expiry_unparsed:{expiry}", ts=ts)

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
            self._skip(signal, ts, f"ignored:{signal.ignore_reason.value if signal.ignore_reason else 'unknown'}",
                       latency_ms)
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
        self.fills.record(event="halt", contract_key="", qty=0, price=0.0,
                          reason="stand-down", ts=ts, latency_ms=latency_ms,
                          note=signal.raw[:120])
        return []

    def _on_entry(self, signal: Signal, quote: Quote | None, ts: float,
                  latency_ms: int | None) -> list[OrderIntent]:
        if self.halted:
            return self._reject(signal, ts, "halted:stand-down", latency_ms)
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
        quote = self._quote_for(key, quote)
        if quote is None:
            return self._reject(signal, ts, "no_quote", latency_ms)
        if far_off_bid(signal.his_bid, quote.ask):
            return self._skip(signal, ts,
                              f"too_far_off_bid:{pct_off_bid(signal.his_bid, quote.ask):.1f}%",
                              latency_ms)

        first_of_day = self.stats.trades_today == 0
        tier = tier_for_channel(signal.channel,
                                is_first_of_day=first_of_day and self.config.pipe_test)
        limit = marketable_limit(signal.his_bid, quote.ask)
        if tier is Tier.PIPE:
            qty = PIPE_TEST_CONTRACTS
        else:
            qty = contracts_for(clip_budget(tier), limit)

        # The governor can only shrink the locked clip. It is consulted AFTER the
        # locked size exists, so "reduce-only" is structural rather than a promise:
        # apply_contracts() also clamps, in case the policy was hand-edited.
        if self.governor is not None:
            decision = self.governor(self.governor_state, signal, ts)
            qty = apply_contracts(qty, decision.factor)
            if decision.stand_down or qty <= 0:
                # A stand-down is a decision worth recording, never a silent drop.
                return self._skip(signal, ts, f"governor:{decision.reason_text}", latency_ms)

        position = Position(contract_key=key, ticker=signal.ticker, side=signal.side,
                            contracts=qty, avg_price=limit, opened_ts=ts,
                            channel=signal.channel)
        self.positions[key] = position
        self.stats.trades_today += 1
        self.stats.intents += 1
        intent = OrderIntent(
            action="buy", qty=qty, limit=limit, contract_key=key,
            reason=f"entry:{tier.value}:his_bid={signal.his_bid:g}:ask={quote.ask:g}",
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
        if self.blocked():
            return self._reject(signal, ts, "halted:kill-switch", latency_ms, key)
        if position.adds >= self.config.add_allowed_per_position:
            return self._skip(signal, ts, "add_already_used", latency_ms, key)
        price = signal.price if signal.price is not None else signal.his_bid
        if price is None:
            return self._reject(signal, ts, "no_price_for_add", latency_ms, key)
        quote = self._quote_for(position.contract_key, quote)
        if quote is None:
            return self._reject(signal, ts, "no_quote", latency_ms, key)
        limit = marketable_limit(price, quote.ask)
        tier = tier_for_channel(signal.channel or position.ticker, is_first_of_day=False)
        qty = contracts_for(clip_budget(tier, is_add=True), limit)
        total = position.contracts + qty
        position.avg_price = (position.avg_price * position.contracts + limit * qty) / total
        position.contracts = total
        position.adds += 1
        self.stats.intents += 1
        intent = OrderIntent(action="buy", qty=qty, limit=limit, contract_key=key,
                             reason=f"add:{tier.value}:{position.adds}/{self.config.add_allowed_per_position}",
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
        quote = self._quote_for(position.contract_key, quote)
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
            closed_note = "already_closed:never_chase" if signal.contract_key in self.closed else "close_without_position"
            return self._skip(signal, ts, closed_note, latency_ms, key)
        qty = position.contracts
        quote = self._quote_for(position.contract_key, quote)
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

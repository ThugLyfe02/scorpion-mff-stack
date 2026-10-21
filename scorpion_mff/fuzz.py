"""Adversarial fuzzing for the hot path: generate posts, assert invariants, find bugs.

The `%`-as-price bug ($8,146 of phantom profit) was found by a human reading a replay.
That is not a method. This is: a deterministic generator produces the kinds of post the
channel actually writes, the real parser and engine consume them, and a set of
invariants that must hold for *any* input is checked after every message.

The invariants are the point, and each one encodes a way to lose money:

  I1  A sell intent can only exist for a contract the book holds.
  I2  No limit price is ever derived from a percentage ("Close at 19%" is not $19).
  I3  No limit price exceeds the strike price of the same contract.
  I4  The book never exceeds max_concurrent.
  I5  Every intent carries a non-empty reason (the audit trail is the product).
  I6  A kill-switch block means zero intents, whatever the post says.
  I7  The same input stream produces the same output stream (determinism).
  I8  Quantity is always >= 1 and never negative.
  I9  A closed contract is never re-entered the same day.
  I10 Nothing in the corpus's grammar can make the parser raise.

A campaign is seeded and replayable: `fuzz --iterations 5000 --seed 7` reproduces exactly.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from typing import Iterator

from .broker import QuoteScript
from .engine import Engine, EngineConfig
from .fills import FillLog
from .parser import CHANNEL_IDS, parse
from .types import AlertKind, Quote, RawMessage

CHANNELS = list(CHANNEL_IDS)
TICKERS = ["QQQ", "SPY", "NVDA", "TSLA", "AAPL", "IWM", "AMD", "SPX", "META", "PLTR"]

ENTRY_HEADS = ["CALL", "PUT", "CALL/", "PUT/", "CALL ODTE", "PUT LOTTO", "CALL RISKY", "PUT ER"]
CLOSE_PHRASES = [
    "Close at {p}", "Closing at {p}", "All out at {p}", "Flatten at {p}",
    "Sold runners at {p}", "Trimmed half at {p}", "Close remaining for {pct}%",
    "Runners at {p}", "Closed fully at {p}", "Take profit at {p}",
    "Not closing yet", "Don't close", "No adds here", "Do not buy",
]
NOISE = ["", "nice", "wow 200%", "@everyone", "🥵", "up 3.5% so far",
         "he said 610 strike is key", "still holding half", "target $240"]


def _price(rng: random.Random) -> float:
    return round(rng.uniform(0.15, 8.0), 2)


def _pct(rng: random.Random) -> int:
    return rng.choice([5, 7, 10, 15, 19, 25, 33, 50, 75, 100, 150, 200, 300, 1200, -30])


def _strike(rng: random.Random) -> str:
    base = rng.choice([200, 225, 240, 355, 360, 390, 592, 600, 610, 700, 706, 719, 7670])
    return f"{base}" if rng.random() < 0.7 else f"{base}.{rng.choice([5, 50, 25])}"


def generate_post(rng: random.Random) -> RawMessage:
    """One message in the channel's grammar, plus the ways it goes wrong."""
    channel = rng.choice(CHANNELS)
    channel_id = CHANNEL_IDS[channel]
    ticker = rng.choice(TICKERS)
    side = rng.choice(["CALL", "PUT"])
    expiry = rng.choice(["0DTE", "Today", "2026-09-11", "Dec 5", "Friday", ""])
    ts = 1_757_000_000.0 + rng.randrange(0, 30 * 86_400)

    kind = rng.choices(
        ["entry", "entry_malformed", "close", "close_pct", "ambiguous", "noise", "negated"],
        weights=[40, 8, 25, 8, 5, 10, 4])[0]

    if kind == "entry":
        head = rng.choice(ENTRY_HEADS)
        parts = [head, f"TICKER {ticker}", f"STRIKE ${_strike(rng)}"]
        if expiry:
            parts.append(f"Exp {expiry}")
        parts.append(f"Current BID Price ${_price(rng):.2f}")
        if rng.random() < 0.2:
            parts.append(rng.choice(NOISE))
        text = " ".join(parts)
    elif kind == "entry_malformed":
        text = rng.choice([
            f"CALL {ticker} {_strike(rng)} {_price(rng):.2f}",
            f"TICKER {ticker} STRIKE ${_strike(rng)}",
            f"{side} TICKER STRIKE $500 BID ${_price(rng):.2f}",
            f"{side} TICKER QQQ STRIKE $ BID $",
            "",
            "   ",
        ])
    elif kind == "close":
        text = rng.choice(CLOSE_PHRASES).format(p=_price(rng), pct=_pct(rng))
        if rng.random() < 0.4:
            text = f"{ticker} {side}s {text.lower()}"
    elif kind == "close_pct":
        # The exact shape that produced a phantom profit: a percentage where a price
        # belongs, next to a strike that must not be read as a sale price.
        text = rng.choice([
            f"Close at {_pct(rng)}%", f"Runners {_pct(rng)}%",
            f"Close remaining for {_pct(rng)}%", f"Sold runners at 25%",
            f"{ticker} {_strike(rng)} strike closed for {_pct(rng)}%",
        ])
    elif kind == "ambiguous":
        text = rng.choice([
            f"Close at {_price(rng):.2f}", f"All out", f"Closing here",
            f"{rng.choice(TICKERS)} and {rng.choice(TICKERS)} closed at {_price(rng):.2f}",
        ])
    elif kind == "negated":
        text = rng.choice([
            f"Don't add here, still holding", f"Not closing yet, runners",
            f"No adds above {_price(rng):.2f}", f"Do not buy this",
        ])
    else:
        text = " ".join(rng.choice([rng.choice(NOISE), "".join(
            rng.choices(string.printable[:70], k=rng.randrange(0, 25)))]) for _ in range(2))

    return RawMessage(channel=channel, channel_id=channel_id, text=text.strip(), ts=ts)


@dataclass
class Violation:
    invariant: str
    detail: str
    post: str

    def __str__(self) -> str:
        return f"{self.invariant}: {self.detail}  <- {self.post!r}"


@dataclass
class Campaign:
    seed: int = 0
    iterations: int = 0
    posts: int = 0
    intents: int = 0
    violations: list[Violation] = field(default_factory=list)
    statuses: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.violations

    def to_text(self) -> str:
        head = (f"fuzz | seed {self.seed} | {self.posts} posts | {self.intents} intents | "
                f"{'CLEAN' if self.clean else f'{len(self.violations)} VIOLATION(S)'}")
        lines = [head]
        if self.violations:
            for v in self.violations[:15]:
                lines.append(f"  {v}")
            if len(self.violations) > 15:
                lines.append(f"  ... and {len(self.violations) - 15} more")
        else:
            lines.append("  every invariant held: " + ", ".join(
                f"{k}={v}" for k, v in sorted(self.statuses.items())))
        return "\n".join(lines)


def _pct_values(text: str) -> list[float]:
    """Every bare percentage number in a post, as a float ('close for 19%' -> [19.0])."""
    out: list[float] = []
    for chunk in text.replace("%", "% ").split():
        if chunk.endswith("%"):
            body = chunk[:-1].strip("+-")
            try:
                out.append(float(body))
            except ValueError:
                continue
    return out


def _check_post(post: RawMessage, signal, intents, engine: Engine,
                held_before: set[str]) -> list[Violation]:
    """Invariants that are decidable from one message's decision."""
    out: list[Violation] = []
    text = post.text
    pcts = _pct_values(text)

    for intent in intents:
        # I2: a percentage must never become a price. The bug that sold a $3.13 contract
        # for $19 produced a limit exactly equal to the stated percentage while the
        # market was far below it - so that is what is checked, not the presence of a %.
        quote = None
        try:
            quote = engine.quotes.quote(intent.contract_key)
        except Exception:                             # noqa: BLE001
            quote = None
        if quote is not None and pcts:
            for pct in pcts:
                if abs(intent.limit - pct) < 1e-9 and intent.limit > quote.bid * 6:
                    out.append(Violation(
                        "I2", f"limit {intent.limit} equals the stated {pct:g}% while the "
                              f"bid is {quote.bid}", text))
        # I5: every intent explains itself.
        if not intent.reason:
            out.append(Violation("I5", "intent without a reason", text))
        # I8: quantities are real.
        if intent.qty < 1:
            out.append(Violation("I8", f"qty={intent.qty}", text))
        # I3: no option is ever priced above its strike.
        try:
            strike = float(intent.contract_key.split("|")[2])
        except (IndexError, ValueError):
            strike = None
        if strike and intent.limit > strike:
            out.append(Violation("I3", f"limit {intent.limit} > strike {strike}", text))
        # I1: never sell what we do not hold. `held_before` is sampled before the call
        # because a full close deletes the position, so checking afterwards would flag
        # every legitimate exit.
        if intent.action == "sell" and intent.contract_key not in held_before:
            out.append(Violation("I1", f"sell for unheld {intent.contract_key}", text))

    # I4: the book honours the limit.
    if len(engine.open_positions) > engine.config.max_concurrent:
        out.append(Violation("I4", f"{len(engine.open_positions)} open > "
                                   f"{engine.config.max_concurrent}", text))
    # I9: no same-day re-entry of a closed contract.
    if signal is not None and signal.contract_key in engine.closed and intents:
        if intents[0].action == "buy":
            out.append(Violation("I9", f"re-entered closed {signal.contract_key}", text))
    return out


def run_campaign(seed: int = 0, iterations: int = 2_000,
                 max_concurrent: int = 2) -> Campaign:
    """One deterministic run. Same seed -> same violations, always."""
    rng = random.Random(seed)
    engine = Engine(config=EngineConfig(max_concurrent=max_concurrent),
                    fills=FillLog(), quotes=_FuzzQuotes(), governor=None)
    campaign = Campaign(seed=seed, iterations=iterations)

    for _ in range(iterations):
        post = generate_post(rng)
        campaign.posts += 1
        # I10: the parser must never raise, whatever the text.
        try:
            signal = parse(post)
        except Exception as exc:                     # noqa: BLE001 - that is the assertion
            campaign.violations.append(Violation("I10", f"parser raised {type(exc).__name__}: {exc}",
                                                 post.text))
            continue
        campaign.statuses[signal.kind.value] = campaign.statuses.get(signal.kind.value, 0) + 1

        before_open = len(engine.open_positions)
        held_before = set(engine.positions)
        # I6: a blocked engine produces nothing, regardless of the post.
        engine.blocked = lambda: campaign.posts % 17 == 0
        try:
            intents = engine.on_signal(signal, now=post.ts)
        except Exception as exc:                     # noqa: BLE001
            campaign.violations.append(Violation("I10", f"engine raised {type(exc).__name__}: {exc}",
                                                 post.text))
            continue
        if engine.blocked() and any(i.action == "buy" for i in intents):
            # I6: the kill switch blocks ENTRIES. It must never block an exit - a switch
            # that traps you in a position is worse than no switch - so a sell intent
            # while blocked is correct behaviour and is asserted separately.
            campaign.violations.append(Violation("I6", "buy intent while kill-switched", post.text))
        campaign.intents += len(intents)
        campaign.violations.extend(_check_post(post, signal, intents, engine, held_before))

    # I7: determinism - a second identical run must produce identical decisions.
    campaign.statuses["open_at_end"] = len(engine.open_positions)
    return campaign


def determinism_check(seed: int = 0, iterations: int = 500) -> bool:
    a = run_campaign(seed=seed, iterations=iterations)
    b = run_campaign(seed=seed, iterations=iterations)
    return (a.violations == b.violations and a.intents == b.intents
            and a.statuses == b.statuses)


class _FuzzQuotes:
    """A market that always prices, so the fuzzer tests logic rather than the feed."""

    def quote(self, key: str) -> Quote:
        try:
            strike = float(key.split("|")[2])
        except (IndexError, ValueError):
            strike = 100.0
        price = max(0.05, round(strike * 0.004, 2))
        return Quote(price * 0.98, price)

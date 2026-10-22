# Hot path — coded, event-driven, no LLM in the exit

Built 2026-10-15 against the 9/8 post-mortem in `docs/HANDOFF.md` and the locked
rules in `live/RULES.lock.md`. This is the "**Code missing (build this)**" list
from the handoff, implemented:

| Handoff asked for | Where it lives |
|---|---|
| Discord Gateway listener | `scorpion_mff/listener.py` (`WebsocketGateway`) |
| deterministic parser | `scorpion_mff/parser.py` |
| state machine off the chat queue | `scorpion_mff/engine.py` |
| RH adapter | `scorpion_mff/broker.py` (`RobinhoodAgenticBroker`) |
| fill log | `scorpion_mff/fills.py` |
| healthcheck | `scorpion_mff/health.py` (latency budget + deadman heartbeat) |
| kill switch | `scorpion_mff/killswitch.py` + `live/NO_TRADE` |

Zero third-party dependencies for everything except the live websocket transport
(optional `websockets`). No shell, ever: argv lists and `urllib` only.

## Why this shape

The 9/8 failure was not a bad trade. The arm cron woke **31 minutes late**
(09:56:26 for a 9:25 arm), a deadman job starved the queue for 579s, 2 of 15
five-minute slots fired, and the day filled **$0** against a **~$295-315**
counterfactual. Two alerts went by: QQQ 719C 0DTE (+10% by 09:56) and NVDA 227.5P
Sep11 (+19% by ~10:05).

Everything here exists to remove that failure mode: one websocket event is
parsed, decided and sent inline, on the same tick, with the latency measured per
event rather than hoped for. There is no agent turn, no shared chat queue, no
scheduler to be starved.

## Locked rules, as implemented

| Rule (live/RULES.lock.md) | Implementation | Test |
|---|---|---|
| `limit = min(live_ask, his_bid * 1.15)` | `pricing.marketable_limit` (floored to the cent) | `test_pricing.py` |
| skip if `> +25%` off his bid | `pricing.far_off_bid` (strictly greater; boundary tested) | `test_pricing.py` |
| first trade of day = 1 contract pipe test | `sizing.contracts_for` + `EngineConfig.pipe_test` | `test_engine.py` |
| king/etf/platinum $1500 (add $2250) | `sizing.clip_budget` | `test_plumbing.py` |
| high-confidence $3000 (add $4500) | `sizing.clip_budget` | `test_plumbing.py` |
| max 2 concurrent | `engine._on_entry` | `test_engine.py` |
| add once, only on an added/avg post | `engine._on_add` | `test_engine.py` |
| close/trim/%/runners → trim to 1 + BE | `engine._on_trim` | `test_engine.py` |
| never chase a closed contract | `engine.closed` set | `test_engine.py` |
| STOP/stand-down = halt (exits still allowed) | `engine._on_stop` / `halted` | `test_engine.py` |
| event-driven, not cron | `listener.Listener.handle` | `test_listener.py` |

## Run it

```bash
python -m venv .venv
.venv/Scripts/python -m pip install pytest            # tests only
.venv/Scripts/python -m pytest tests -q               # 358 tests, offline

.venv/Scripts/python -m scorpion_mff.cli doctor       # rules, switches, broker mode
.venv/Scripts/python -m scorpion_mff.cli replay tapes/sep8.json
.venv/Scripts/python -m scorpion_mff.cli arm          # DRY: decides, places nothing
.venv/Scripts/python -m scorpion_mff.cli status       # kill switch + fill-log tail
```

`arm` refuses without `DISCORD_BOT_TOKEN`. `--live` additionally needs
`MFF_RH_TOKEN` **and** a configured quote feed. Dry is the default everywhere:
`RobinhoodAgenticBroker(dry_run=True)`.

### The 9/8 replay (the proof)

```
replay tapes/sep8.json  (pipe_test=True)
events 4 | buys 2 (6 contracts) | sells 2 | realised $211.00
latency worst 0ms  budget 10000ms  breaches 0
  buy   1x QQQ|CALL|719|0DTE         @ 1.05   entry:pipe
  sell  1x QQQ|CALL|719|0DTE         @ 1.11   close:pct=10%
  buy   5x NVDA|PUT|227.5|2026-09-11 @ 2.72   entry:standard
  sell  5x NVDA|PUT|227.5|2026-09-11 @ 3.13   close:pct=19%
```

`--no-pipe-test` reproduces **$289** on the same tape, which is the post-mortem's
~$295-315 counterfactual: **the pipe test is what costs the ~$78 difference.** That
is a fact about the rules, not a bug — worth deciding on deliberately.

## Status, honestly

**Proven offline:** parser against 23 real posts (including the OCR junk the
backtest corpus is full of), pricing boundaries, the full sizing table, every
state-machine transition, the fill log, the kill switch, the latency budget,
the HTTP order shape (against a real local server: idempotency key, retry ladder,
429 honouring `retry_after`, token never in an error), and the 9/8 replay.

**Not yet proven — needs your credentials or a decision:**

1. **The Robinhood Agentic order endpoint.** `POST {base}/orders` with a JSON
   body is what the adapter sends; the real path and field names are yours to
   confirm (`MFF_RH_BASE`). I did not invent a vendor schema.
2. **The quote feed.** `HttpQuoteProvider` takes a URL template plus the dotted
   JSON paths to bid/ask (`MFF_QUOTE_URL_TEMPLATE`, `MFF_QUOTE_BID_PATH`,
   `MFF_QUOTE_ASK_PATH`). Unconfigured, every entry fails closed with `no_quote`
   — correct, but useless until it points at a real feed. `arm --live` refuses
   without it.
3. **The Gateway.** Needs a bot token with the **MESSAGE_CONTENT** privileged
   intent enabled in the Discord developer portal, plus guild membership for the
   four channel ids.
4. **LOTTO policy.** Posts flagged LOTTO (a lottery play, e.g. `CALL/ ODTE LOTTO
   TICKER TSLA`) are IGNORED by default. The lock names lotto-* CHANNELS as
   excluded; whether a LOTTO-flagged post inside `options-king` is also out is a
   house call. Flip it in `parser.parse` when you decide.

## Defects found and fixed (why the replay harness and a second opinion exist)

**Caught by the replay harness:**

* `Close at 19%` parsed **19.00 as a price** and sold a $3.13 contract for $19 —
  an $8,146 phantom profit that was pure parser error. Price extraction now
  refuses a number followed by `%`, and refuses `$240 strike` (a strike mention).
* An exit that restates no contract (`Close remaining for 10%`) got **no quote**,
  so a legitimate close was rejected. The engine now resolves the position first
  and prices *that*.

**Caught by an independent review** (`just fh-stack .pi/fusion-harness/model-stack-lean.yaml -p …`,
architect deepseek-v4-pro · main deepseek-v4-flash), all now regression-tested in
`tests/test_review_fixes.py`:

| Finding | Fix |
|---|---|
| `SOLD RUNNERS` flattened the position, but the lock lists "sold runners" under trim-to-1+BE | precedence inverted: unambiguous finality phrases ("closed fully", "flatten") first, then any scale-out word |
| `SCALE` meant trim, so "scaling in" cut a position he was building | `SCALING IN`/`SCALE IN` are ADDs; only `SCALE OUT`/`SCALING OUT` trim |
| A negated verb still fired: "don't add", "not closing yet", "no adds above 1.50" | a negated verb is IGNORE; `DO NOT BUY` stays a stand-down |
| No dedup: a Gateway RESUME redelivery would trade the same post twice, and the second buy has a fresh idempotency key so the broker cannot catch it | the listener drops a repeated `message_id` (bounded 5,000) and logs `duplicate` |
| `closed` never reset, so a 0DTE key could never be re-traded on a later day | daily roll on the UTC date: clears `closed`, resets the pipe counter, logs `day_roll` |
| An ambiguous exit (two positions in one channel, no contract named) was a silent skip | logged as an `alert` event — a human decides; the automation still refuses to guess |

**Raised by the review and deliberately NOT changed** (your call, not mine):

1. **A bare `%` post still trims.** "up 15% today" is commentary that will trim a
   runner. The lock says a Discord `%` is an exit signal, and missing a real exit
   is the 9/8 failure mode — so the bias is deliberate. If loud commentary becomes
   a nuisance, require an exit verb or a runner word on `%` posts.
2. **An exit is a quote-priced limit with no market fallback.** If the quote is
   stale or the feed is down, the exit can sit unfilled. The engine fails closed
   (`no_quote`) rather than guessing a price; a marketable-limit fallback needs a
   risk decision you should make.
3. **A rejected entry is silent beyond the fill log.** The signal is dropped, not
   retried. That is the log's job — `live/fills.jsonl` carries every reject with
   its reason — but nothing pages you.
4. **A channel-only exit can still close the "wrong" single position** when he
   posts an exit in a channel whose only open position is not the one he means.
   Named contracts and ambiguity handling cover most of it; the rest needs his
   actual posting habit.

## Added after driving 93 real alerts (`BACKTEST.md`)

Two rules exist because a single-day tape cannot expose these, and 59 days of real
alerts do:

* **Expiry releases a slot** (`Engine._expire_carried`). MAX_CONCURRENT is a live-risk
  limit, so a position whose contract has provably expired must stop counting. Without
  it, one alert with no recorded exit held both slots and every later entry was skipped —
  the hone goes silent mid-month and the log says only `max_concurrent`. `0DTE` and past
  ISO expiries are released at the day roll; an unparseable expiry (`Dec 5`) is **not**
  released and raises `expiry_unparsed`, because freeing a live slot on a guess
  under-counts risk. The release is `accepted=False`, so an unmarked expiry never enters
  realised P&L.
* **A bare ticker attributes an exit** (`parser._BARE_SYMBOL_RE`). The parser used to
  recover a ticker only from the literal `TICKER X` form, so `QQQ puts closed at 2.85`
  arrived nameless and could only be resolved by channel — and with two positions open in
  one channel, every exit became `ambiguous_exit` and nothing could close. A bare symbol
  is now accepted when exactly one candidate remains after removing English words.
  Ambiguity is still an alert, never a guess: two symbols in one post resolves to nothing.

Both are covered by `tests/test_exit_attribution.py`, including the two-position case.

## Ops: does anyone know when it stops?

The fill log records everything a machine needs, and nothing was reading it. The failure
class that matters is **silence**: a listener that dies at 09:31, or a book that fills with
unexitable positions, produces zero activity — no fills, no errors, nothing to notice.

`watch --once` is one bounded check, meant for Task Scheduler (or cron) every minute or two:

    python -m scorpion_mff.cli watch --once [--stale-after 120] [--dry-run] [--json]

It reads `live/heartbeat.json` and the tail of `live/fills.jsonl`, then:

| condition | status | pages? |
|---|---|---|
| no heartbeat file | DOWN | yes — the listener has not run here |
| heartbeat older than `--stale-after` | STALE | yes — it ran, then stopped/wedged |
| any `alert` event today | ALERTING | yes — the engine refused to act on something |
| fresh heartbeat, no alerts | OK | no — one stat, exit 0 |
| `live/NO_TRADE` present | OK | no — an armed kill switch is a decision, not a fault |

Exit codes: `0` healthy, `1` a problem was found (and paged, or printed in a dry run),
`2` the page could not be delivered. A watchdog that reports success when it could not
deliver is worse than no watchdog.

Paging goes through `scorpion_mff/page.py` — the same bot the stock-finder digest uses —
with the properties that matter: fail closed without both token and chat id, `retry_after`
honoured on 429, 5xx retried once, 4xx never retried, the token redacted from every error
string, and long text chunked on line boundaries so nothing is silently truncated.

Credentials (never committed): `MFF_TELEGRAM_TOKEN` + `MFF_TELEGRAM_CHAT`
(`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are accepted as fallbacks, so one `.env` serves
both desks).

**What it does not do:** it does not restart anything, does not page on position-level P&L,
and cannot tell a quiet market from a dead feed — it knows only whether the process is
beating and whether the engine raised something it could not act on.


## Adversarial hardening (`fuzz`)

The `%`-as-price bug — a $3.13 contract "sold" for $19, $8,146 of phantom profit — was
found by a human reading a replay. That is not a method. `scorpion_mff/fuzz.py` generates
the shapes the channel actually writes (entries, malformed entries, closes stated as
prices *and* as percentages, negations, multi-ticker closes, noise) and asserts ten
invariants that must hold for **any** input, after every message:

    I1  a sell only for a contract the book holds        I6  a kill switch blocks buys, never exits
    I2  a percentage never becomes a price               I7  same input -> same output
    I3  no option priced above its strike                I8  qty >= 1, never negative
    I4  the book never exceeds max_concurrent            I9  a closed contract is not re-entered same-day
    I5  every intent carries a reason                   I10 the parser never raises

    python -m scorpion_mff.cli fuzz --iterations 4000 --seed 7     # exit 1 on a violation

18,000 generated posts across six seeds run clean, and the campaign is deterministic, so
CI can gate on it.

**It found two real defects on its first runs, and both were money-losing:**

1. **A named exit for a contract we do not hold closed a different one.** `PLTR CALLs
   closing at 7.76` flattened a held NVDA put, because NVDA was the only position in that
   channel: `_resolve` fell through to channel-only resolution even though the post named
   a contract. Now a post that names a ticker or a side must match one position or
   nothing; the channel fallback applies only to a post that names nothing at all.
2. **The kill-switch invariant itself was wrong** — the first version failed any intent
   while blocked, which flagged a legitimate *exit*. The engine was right and the fuzzer
   was wrong: a switch that traps you in a position is worse than no switch. Entries are
   blocked; exits are not, and that is now pinned by a test.

Two of the invariants were also corrected in the other direction (I1 had to sample the
book *before* the decision, because a full close deletes the position; I2 had to compare
the limit against the stated percentage *and* the live bid rather than merely detect a
`%`). A fuzzer whose invariants are wrong is a noise generator, so the invariants are
tested as carefully as the code they guard.

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

## The Gateway is a session, not a socket

A websocket drop used to end the async generator, which ended the pump, which stopped the
heartbeat and made the hone silent until a human noticed. That is the 9/8 failure wearing
a transport costume. `scorpion_mff/gateway.py` keeps the Discord session across sockets and
`listener.WebsocketGateway` consumes it:

| Invariant | Why it costs money when wrong | Test |
|---|---|---|
| Every DISPATCH sequence is tracked | RESUME replays from the last sequence, so losing it replays the wrong window | `tests/test_gateway.py` |
| The heartbeat carries the last sequence | A heartbeat with no `s` cannot resume at the right point | `test_heartbeat_carries_the_last_sequence_and_tracks_the_ack` |
| An unACKed heartbeat is a zombie | A half-dead socket looks connected and delivers nothing | `test_a_missing_heartbeat_ack_is_a_zombie...` |
| A sequence gap is paged | A missed `MESSAGE_CREATE` is a missed exit, not a cosmetic counter | `test_the_socket_loop_pages_a_gap_and_skips_a_duplicate` |
| A redelivery is skipped by sequence | The listener's message-id dedupe is the second fence, not the first | `test_a_redelivered_sequence_is_duplicate...` |
| RESUME after op 7 / resumable op 9 | A re-IDENTIFY starts a new session and drops everything buffered | `test_identify_then_resume_carries_session_and_last_sequence` |
| Non-resumable closes force IDENTIFY | 4007/4009 cannot be replayed; pretending otherwise stalls forever | `test_unresumable_close_codes...` |
| Fatal closes stop and page | Reconnecting on 4004 (bad token) is an infinite silent loop | `test_fatal_close_codes_stop_instead_of_reconnecting` |

`arm` writes a `gateway_reconnect` row for a routine reconnect and a `gateway_gap` /
`gateway_fatal` **alert** row for the cases a human must see. `watch --once` already pages
on alert rows, so the transport has one observability path, not two.

```bash
.venv/Scripts/python -m pytest tests/test_gateway.py -q     # 20 tests, offline
```

What this does not prove: a real Discord handshake. The state machine and the socket loop's
frame handling are exercised with a scripted socket; the token, intents and guild membership
still have to be real. The lifecycle is no longer the unproven part.

## The lane boundary: PR #8 is live, PR #1 is paper

Two PRs, two jobs, one gate. The boundary is not a preference; it is what keeps a
research component from becoming a live dependency by accident.

| | **PR#2 — this tree** | **PR#1 — `.worktrees/pr1`** |
|---|---|---|
| Branch | `hardening/correlated-book-guard` | `hardening/deterministic-control-plane` |
| Job | live Discord -> Robinhood executor | event-sourced control plane / research frontier |
| May place an order? | yes, `arm --live` only, after the policy/token/quote refusals | **no** — paper plane |
| Imports the other lane? | never | never |
| 3.a gate | ruff clean, mypy 28 files, **536 passed** (513 at 3.a; +3 transport, +20 gateway) | ruff clean, mypy 63 files, **551 passed + 1 xfailed** |

The merge boundary is explicit:

* **PR#2 stays the live lane.** It is the only tree that can place an order, and it
  places one only through `arm --live` after `arm` has refused on any missing precondition
  (`doctor` shows that surface first).
* **PR#1 remains the paper plane.** It is read-only source for this lane; it is never a
  runtime dependency and it never places an order.
* **No cross-imports.** Nothing under `scorpion_mff/` may `import scorpion`, add the pr1
  tree to `sys.path`, or reach into `.worktrees/`. Nothing under `.worktrees/pr1/src/`
  may import `scorpion_mff`. A port is copy-and-harden plus a test in this tree, never a
  dependency.
* **No worktree edits.** This lane does not edit `.worktrees/pr1`; 3.a left
  `git -C .worktrees/pr1 status --short` empty, and this lane keeps it that way. A change
  to the paper plane is a change in PR#1, reviewed and gated there.
* **They merge independently.** PR#1 does not merge into PR#2, and PR#2 does not merge
  into PR#1. Each carries its own gate.

Check the boundary without running anything that trades:

```bash
grep -rnE "^[[:space:]]*(from scorpion[.]|import scorpion$)|worktrees" scorpion_mff
grep -rnE "scorpion_mff|\.\./\.\./scorpion" .worktrees/pr1/src
```

Both should print nothing. (3.a item 10 records the same result.)

## PR#1 -> PR#2 port matrix (read-only source)

What crossed the lane, in which direction, and what did **not** cross. The PR#1 paths are
read-only source modules; the PR#2 paths are the live port. Verdicts are 3.a's, quoted
verbatim in `docs/GEMS.md`.

| PR#1 read-only source | What it owns | PR#2 live port | Verdict |
|---|---|---|---|
| `.worktrees/pr1/src/scorpion/sessions.py:16-62` | ET session, holiday and half-day tables, DST offset | `scorpion_mff/calendar.py` (`OPEN_ET`/`CLOSE_ET`/`HALF_DAY_CLOSE_ET` 31-33, `et_offset_at` 93-120, `et_date` 129-138, `is_trading_day` 175-186) | **Ported + hardened.** Unknown year raises before the weekend check; the DST seam is dated by instant, not by a per-date approximation. |
| `.worktrees/pr1/src/scorpion/expiry.py:96-135` | release a carried position only when its expiry is provable | `scorpion_mff/engine.py:329-393` (`_expire_carried`) | **Ported as a superset.** Same `< market_day` rule, plus same-day tokens and a mandatory `expiry_unresolved:...:assignment=` alert; an unreadable key keeps the slot. |
| `.worktrees/pr1/src/scorpion/outbox.py:78-84` | circuit breaker: threshold, cooldown, one probe | `scorpion_mff/execution.py:44-111` (`CircuitBreaker`) + `listener.py:131-197` (`_place`) | **Ported + stricter.** PR#1 admitted unbounded half-open probes; PR#2 arms exactly one via `_probe_armed`. The breaker gates buys only. |
| `.worktrees/pr1/src/scorpion/guard.py:44-45` | "UNEVALUABLE blocks" | `scorpion_mff/execution.py:114-131` (`spread_ratio`/`spread_ok`) | **Philosophy ported, check set NOT ported.** `guard.evaluate`/`DEFAULT_CHECKS` (drift/vol/tail/burst/cooldown) have no live equivalent; see the unported-surfaces table in `docs/GEMS.md`. |
| `.worktrees/pr1/src/scorpion/reconciliation.py:150-166` | three-valued venue answer (`seen`/`not_seen`/`unknown`) | `scorpion_mff/broker.py:37-48`, `:214-249` (`OrderLookup`) + `ledger.py` | **Tri-state ported; drift-halt NOT ported.** `reconcile`/`reconcile_and_halt` (`phantom_local`/`missing_local`/`quantity_mismatch`) still have no live detector; that is O2. O1's log/ledger seam is closed, so an ambiguous order is now visible and resolvable, but there is no automatic venue snapshot to halt on. |
| `.worktrees/pr1/src/scorpion/outbox.py:181-188` | quarantine a sender exception as UNKNOWN for later reconciliation | `scorpion_mff/listener.py` `_on_transport_failure` + `ledger.py` (`TRANSPORT_UNKNOWN`, `quarantine_reasons`) | **Ported in final integration.** A transport failure writes a non-terminal `transport_unknown` row; the intent stays unresolved for `verify`/`recover`. An unknown **exit** pages immediately (`exit_unknown:transport`), because the engine books flat at intent time. Nothing retries. |

Live-lane additions with no PR#1 counterpart (they are not ports, and are not claimed as
such):

| PR#2 surface | What it adds |
|---|---|
| `scorpion_mff/expiry.py` + `engine.expiry_intents` + `listener.tick` | the 15-minute cutoff: `HOLD`/`FORCE_CLOSE`/`HALT_AND_PAGE`, at a live bid or a page, on a clock that fires with no Discord traffic |
| `scorpion_mff/cli.py` `recover` | resolve a crash-left intent from a venue lookup; never places, never retries |
| `scorpion_mff/cli.py` `expiry audit` | measure the corpus against the cutoff and refuse an assignment projection the corpus cannot support |

Full acceptance ledger, one command per gem: **`docs/GEMS.md`**.

## Run it

```bash
python -m venv .venv
.venv/Scripts/python -m pip install pytest            # tests only
.venv/Scripts/python -m pytest tests -q               # 560 tests, offline

.venv/Scripts/python -m scorpion_mff.cli doctor       # rules, switches, broker mode,
                                                      #   spread-gate setting (OFF unless armed)
.venv/Scripts/python -m scorpion_mff.cli replay tapes/sep8.json
.venv/Scripts/python -m scorpion_mff.cli expiry audit # 40/93 same-day, 65/93 no close,
                                                      #   0/40 force-close, assignment REFUSED
.venv/Scripts/python -m scorpion_mff.cli recover      # resolve crash-left intents from a
                                                      #   lookup; never places
.venv/Scripts/python -m scorpion_mff.cli verify       # fill-log integrity + unresolved intents
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
429 honouring `retry_after`, token never in an error), and the 9/8 replay. Since the
PR#1 ports, the ET calendar, the 0DTE cutoff matrix, the one-probe breaker, the tri-state
ledger, and `recover` are also proven offline; `docs/GEMS.md` is the one-command index.

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
   four channel ids. The session lifecycle (RESUME, heartbeat ACK, sequence gaps,
   fatal closes) is now proven offline against a scripted socket; only the real
   handshake and credentials remain.
4. **LOTTO policy.** Posts flagged LOTTO (a lottery play, e.g. `CALL/ ODTE LOTTO
   TICKER TSLA`) are IGNORED by default. The lock names lotto-* CHANNELS as
   excluded; whether a LOTTO-flagged post inside `options-king` is also out is a
   house call. Flip it in `parser.parse` when you decide.
5. **Automatic venue-side reconciliation (3.a O2).** A transport-failed order is now
   quarantined rather than closed with a fake `reject`: the intent stays unresolved, an
   unknown **exit** pages immediately (`exit_unknown:transport`), `verify` names the
   ambiguity, and `recover` settles it from a venue lookup. What does not exist is an
   automatic book-vs-venue **drift halt** — there is no venue positions snapshot to compare
   against on a schedule, so a human (or `verify`/`recover`) must act on the page. This is
   unported, not hidden; do not "fix" it by making `recover` retry. See `docs/GEMS.md` for
   the recorded risk and the gate that would have to open.

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
| `closed` never reset, so a 0DTE key could never be re-traded on a later day | daily roll on the ET trading date (`calendar.py`): clears `closed`, resets the pipe counter, logs `day_roll` |
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
  the hone goes silent mid-month and the log says only `max_concurrent`. Same-day tokens
  (`0DTE`/`ODTE`/`TODAY`) and past ISO expiries are released at the ET trading-date roll;
  an unparseable expiry (`Dec 5`) is **not** released and raises `expiry_unparsed`,
  because freeing a live slot on a guess under-counts risk. The release is
  `accepted=False`, so an unmarked expiry never enters realised P&L — and it is never
  silent: it also writes `expiry_unresolved:<why>:<expiry>:assignment=<shares|unknown>`.
* **The close is a clock, not a post** (`Engine.expiry_intents`, `Listener.tick`). A 0DTE
  is flattened at the bid actually seen inside the cutoff window (15 minutes before the
  session's own close, so 12:45 on a half day), or paged `HALT_AND_PAGE` and left on the
  book when there is no live bid. Off unless `EngineConfig.enforce_expiry` (replay parity);
  `arm` turns it on. Measured against the corpus by `expiry audit`.
* **A bare ticker attributes an exit** (`parser._BARE_SYMBOL_RE`). The parser used to
  recover a ticker only from the literal `TICKER X` form, so `QQQ puts closed at 2.85`
  arrived nameless and could only be resolved by channel — and with two positions open in
  one channel, every exit became `ambiguous_exit` and nothing could close. A bare symbol
  is now accepted when exactly one candidate remains after removing English words.
  Ambiguity is still an alert, never a guess: two symbols in one post resolves to nothing.

Both are covered by `tests/test_exit_attribution.py`, including the two-position case.

## Expiry audit, recover, and the doctor setting

Three commands were added with the PR#1 ports. Each answers a question the fill log
cannot answer by itself; each is safe to run while the hone is live.

| Command | Invariant it enforces | Failure mode it exists for |
|---|---|---|
| `expiry audit` | the measurement refuses claims the corpus cannot support: no underlying close -> **assignment REFUSED**; no recorded exit time -> page, not an invented price | a fabricated assignment number, or a cutoff that force-closes on a price nobody saw |
| `recover` | a crash-left or transport-quarantined `intent` is resolved **only** from a venue lookup: `SEEN` -> `recovered`, `NOT_SEEN` -> `not_sent`, `CANNOT_SAY` -> still unresolved and exit 1; it never places and never retries | a restart that double-sends, or a local book that looks flat while the venue still holds risk |
| `doctor` | every switch is visible before arming, including the spread gate. `OFF (MFF_MAX_SPREAD_RATIO unset)` is printed, not omitted | arming while believing a gate is on when it is off (or the reverse), or a live run that was meant to be dry |

The audit's output on the current corpus is the evidence, not a promise:

```
same-day expiry          40 / 93  (43.0%)
no explicit final close  65 / 93  (69.9%)   floor 61 from the outcomes
cutoff force-close        0 / 40 same-day
   no recorded exit time  10 / 40 - at the cutoff these PAGE
assignment projection   REFUSED - the corpus records the option premium only
clock caveat: the corpus carries no timezone proof
```

`recover` is a lookup, not a retry. Given an unresolved intent it appends exactly one
terminal row from `OrderLookup` and nothing else; a `CANNOT_SAY` answer leaves the row
open and exits 1, because an unknown is not a terminal. A transport failure is kept in the
same unresolved state by a `transport_unknown` quarantine row, and `verify` prints the
cause (`[transport unknown: ...]`); the row is never closed on a guess. The tests pin `placed == 0` in
every branch. `doctor` is read-only: it touches no file and places nothing.

Full gem-by-gem acceptance commands: **`docs/GEMS.md`**.

What is deliberately **not** built, and the gate that would have to open first: no third
parser, no conformal sizing on 93 trades, no latency-to-edge threshold without measured
alert-to-fill drift, and no LLM in the path. The reasons are in `docs/GEMS.md`; the two
unported PR#1 surfaces (guard check set, venue-drift halt) are recorded there too.

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

**What it does not do:** it does not restart anything, and cannot tell a quiet market from
a dead feed — it knows only whether the process is beating and whether the engine raised
something it could not act on. Position-level risk is a different tripwire: `marks --check`
exits 1 when an armed breakeven stop is through or the book cannot be priced, so a cron can
page on the book the way `watch --once` pages on the hone.

## Position stops, marks, and log integrity (`stops.py`)

The lock says a trim leaves "1 contract + breakeven stop". The engine armed that stop
(`Position.breakeven_stop`, and `stop->be@...` in the fill log) but nothing ever read it
back — a later quote below breakeven did nothing, so the stop was a note rather than a
fact. `stops.py` closes that seam in three directions, each a pure function of
(book, quote) so a replay can reproduce every one of them:

* **`breakeven_exit` / `engine.on_quote`** — a bid at or through the armed stop flattens
  the remainder at the bid actually seen, never the stop price, so a gap through the stop
  is priced honestly. Opt-in via `EngineConfig.enforce_breakeven`; `arm --enforce-breakeven`
  turns it on, and `Listener.refresh_stops` polls quotes on the live pump's cadence so the
  stop fires between Discord events, not only when the next post arrives.
* **`marks` (`python -m scorpion_mff.cli marks`)** — marks the open book to a live bid
  (unrealised P&L, at/through-cost and through-stop flags) and **withholds the total** when
  any position cannot be priced, because a partial total presented as "the book's P&L" is
  the same lie the fill log was built to prevent. `marks --check` is the cron tripwire:
  exit 1 when a stop is through or the book cannot be priced.
* **`verify` (`python -m scorpion_mff.cli verify`)** — validates the fill log can be read
  back into the book it describes: every row parses and sequence numbers are contiguous. A
  gap is a lost decision, a duplicate is a double-counted fill, and a torn last line is a
  crash mid-append. `book_from_fills` rebuilds the book (stops included) so `marks` never
  reads a second source of truth.


## Pre-trade risk budget (`risk.py`)

The governor is a **realised** risk layer: it reacts to money already lost, so it cannot
stop the first loss. With two concurrent clips, the worst case before this module was
twice the daily limit — two $3,000 clips against a $3,000 daily stop, both expiring
worthless, and only then does the governor learn. `risk.py` prices the loss *before* the
order.

For a long option the maximum loss is the premium paid, so the book's worst-case day is
`realised_today - open_premium - new_premium`. `RiskBudget.admit` returns the largest
**contract count** that keeps that number inside the daily limit and every other cap
(total premium, per underlying, declared correlation cluster), and the engine consults it
after the locked clip and the governor, before the exposure guard:

* it **shrinks** the clip and never grows it; a refusal is a `skip` row naming the cap;
* the same pass runs on an **add**, against the position already held, so a second buy
  cannot undo the first cap;
* an unmapped symbol is its own cluster, and a symbol declared in two clusters is a
  refused configuration — a guessed correlation is how a diversified book becomes one bet;
* an absent budget leaves every decision byte-identical (the replay/backtest parity rule).

The activation is the file, not a flag: `arm` loads `live/risk-budget.json` when present and
refuses an unreadable budget rather than trading uncapped. `risk init` copies the reviewed
starter, `risk validate` parses every cap, and `risk show` rebuilds the book from the fill
log and prints the worst-case day plus headroom by underlying and cluster.

`docs/GEMS.md` G14 is the acceptance command; `POLICY.md` explains why the cap is
reduce-only and why the cluster map is declared.

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

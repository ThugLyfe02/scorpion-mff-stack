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

> Numbering note: this document was written when the live lane was opened as the
> second pull request, so the text below says "PR#2". Its GitHub number is **#8**;
> "PR#1" is still #1. The branch names are the stable identifiers.

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
.venv/Scripts/python -m pytest tests -q               # 585 tests, offline

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
ledger, and `recover` are also proven offline. Since the confirmed-execution book, the
cumulative ack, the split fill, the oversell clamp, the restart replay and the
`sync_engine` drawdown restore are proven offline too - 708 tests, `docs/GEMS.md` is the
one-command index.

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
   ambiguity, and `recover` settles it from a venue lookup. The confirmed book adds the
   durable side of the same story: an order with no venue answer stays `PENDING`,
   `arm` refuses to start on it, and `book verify --strict` fails on it. The **feed
   adapter** and the **scheduled drift halt** now exist too (`venue.py`,
   `arm --venue-snapshot ... --venue-every 300`): a stamped snapshot the venue published,
   refused when it is stale or undatable, with drift halting new buys. What remains is the
   credentialed part - the real positions endpoint's path and field names
   (`MFF_VENUE_URL`, `MFF_VENUE_POSITIONS_PATH`) are yours to confirm, because this stack
   does not invent a vendor schema. Until then the file feed and the fixture path are
   proven offline.

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

## The add seam: one ladder, or the risk layer has a hole

The entry path ran halt -> kill switch -> governor -> pre-trade budget -> exposure.
The add path ran kill switch only, then mutated the position. That asymmetry is a
money defect, not a style one:

* `STAND DOWN` halted `_on_entry` and left `_on_add` free, so "adding at 1.30" grew a
  position on a desk that had been told to stop;
* a daily stop, loss ladder or channel tilt was enforced on the first clip and ignored
  on the second, so the next add could undo the stop that existed to prevent it.

`_on_add` now runs the same ladder: `halted` -> kill switch -> add-once -> governor ->
`apply_contracts` -> pre-trade budget -> mutate. A refusal names its reason
(`halted:stand-down`, `governor:daily_stop`, `risk_budget:...`) and records a `skip` or
`reject` row before the book changes. Exits are untouched by all of it: the halt gates
buys, never sells, because a switch that traps a position is worse than no switch.

The regression suite is `tests/test_add_safety.py` (G15 in `docs/GEMS.md`).

## Venue reconciliation (`reconcile.py`)

The engine's book is a decision cache. The venue is the truth. Nothing compared them,
so a restart, a rejected buy, a partial fill or a manual trade could leave the bot flat
in memory while the venue still held risk - and no exit would ever be posted for that
position. `reconcile.py` is the missing comparison:

| drift | local | venue | what it means |
|---|---|---|---|
| `phantom_local` | 5 | 0 | the engine will try to sell a contract it does not have |
| `missing_local` | 0 | 5 | real risk the exit path, governor and marks cannot see |
| `quantity_mismatch` | 5 | 2 | a partial fill recorded as a full clip |

A snapshot is evidence. Duplicate keys, negative/boolean/float counts and missing
contract keys are refused rather than summed or clamped; a malformed file is not a
flat book. `Engine.reconcile_positions` consumes a clean report as one `reconcile` row
and drift as `halt_reason=reconcile-drift` + an alert + `halted:reconcile-drift` on new
buys. **Exits stay legal**: a desk that cannot reconcile must still be able to leave a
position it is in.

The operator surface is `reconcile --snapshot SNAP`: exit 0 clean, 1 drift (a page),
2 malformed. The venue *feed* is not built; the snapshot format is
`live/venue-snapshot.example.json`, and requiring it is deliberate - deriving the venue
side from the fill log would compare the book with itself and always pass.

## Confirmed execution: the durable book (`execution_book.py`)

Reconciliation compares two books; the fill log records decisions; the ledger matches an
intent to a terminal. None of them is **state**. Nothing in the stack could answer "what
does the venue's answer say we hold, and how much did we realise?" after a restart, so
four defects were structurally possible: a rejected buy holding a concurrency slot
forever, a partial fill booked as the full clip (overstating realised P&L and equity), a
rejected sell that had already moved the drawdown, and a restart that forgot every open
position.

`scorpion_mff/execution_book.py` is that state. It is append-only (`live/execution.jsonl`,
seven event kinds), replayable, and it is the only thing the engine's book is synced
*from* at arm time.

| Invariant | Why it costs money when wrong | Test |
|---|---|---|
| An ack's `qty` is **cumulative**; only the delta is applied | a 3-then-2 split booked as 3+2 is 6 contracts and 2x the realised R | `test_a_second_ack_applies_only_the_delta_and_averages_the_price` |
| An identical repeat ack applies 0 and is counted | a redelivered broker response becomes a double fill | `test_an_identical_ack_cannot_double_fill` |
| A regressing ack is refused, never clamped | a smaller cumulative means a reused id or a corrupt log | `test_a_regressing_ack_is_refused_rather_than_clamped` |
| A reject is terminal; a late reject cannot undo a fill | the venue did execute, whatever the cancel said | `test_a_late_reject_cannot_undo_a_full_fill` |
| A sell larger than the lot is clamped and counted | a negative lot or invented contracts | `test_a_sell_larger_than_the_lot_is_clamped_and_counted` |
| A sell with no lot realises nothing | this book is long premium only; a short would be a lie about risk | `test_a_sell_with_no_lot_invents_nothing` |
| A fill with no intent is refused | adopting an unseen execution is an operator act, not an auto-create | `test_a_fill_with_no_intent_is_refused` |
| A transport failure is `unknown`, never a terminal | a reject for a send that may have arrived hides the order from `recover` | `test_a_restart_mid_send_leaves_the_order_unresolved` |
| A torn **last** line is ignored; a torn middle line is refused | accepting mid-history corruption silently discards confirmed fills | `test_corruption_in_the_middle_is_refused` |
| `sync_engine` = `baseline + confirmed events` | a restart that resets a drawdown is a max-drawdown rule that does not exist | `test_sync_engine_restores_positions_and_does_not_erase_a_drawdown` |
| A lot carried past expiry is released first | a dead contract holding a slot is the concurrency bug with a restart in front of it | `test_sync_engine_expires_a_lot_the_market_already_took` |
| A trim arms the breakeven stop on the **confirmed** lot | the stop `stops.py` enforces must be the one the fill created | `test_a_trim_arms_the_breakeven_stop_on_the_confirmed_lot` |

The four operator commands, all offline:

```bash
python -m scorpion_mff.cli book show      # the book, its digest, its anomalies
python -m scorpion_mff.cli book replay    # replay twice; exit 1 if the digests differ
python -m scorpion_mff.cli book verify    # structural defects; --strict fails on unresolved
python -m scorpion_mff.cli book sync      # restore a fresh engine from confirmed state
python -m scorpion_mff.cli book export    # confirmed fills -> tca.Execution rows for `tca fills`
```

Two decisions are worth naming because they are refusals, not features:

* **`arm` refuses when an order has no venue answer.** An unresolved order means the book
  is a hypothesis, and arming on a hypothesis is how a restart becomes a double entry.
  `recover` settles it from a lookup; `--allow-unresolved` is the explicit, visible
  override.
* **The expiry assumption is stated, not implied.** With no settlement feed, a carried
  long is marked at **zero** - the maximum loss - and flagged `assumed_zero` in the log.
  `assume_zero=False` releases the slot without manufacturing P&L and counts
  `expired_unsettled`, which is the operator's cue to reconcile.

What this does **not** claim: no real venue fill has been booked yet, so the book is
proven offline (66 tests) and unproven against Robinhood's ack shape. The `tca.Execution`
export exists so that the first real ack prices itself through `tca fills` instead of
being entered by hand.

## Confirmed exits: the running process follows the venue

`sync_engine` made a **restart** honest. The process that never restarted still had the
old hole: the engine deletes a position and books P&L at *sell-intent* time, so a sell
the venue rejected, partially filled, or never answered left the bot believing it was
flat - with the contract in `closed`, and "never chase" refusing to exit again. The lot
was still at the venue and invisible to every guard. `listener.py` now makes the
**running** engine follow the confirmed book after every venue answer.

The invariant is one sentence: **the engine is never more optimistic than the venue's
last answer, and one exit is in flight per contract.** Two deliberately asymmetric
rules make it safe in both directions:

| Answer | Position | Why |
|---|---|---|
| sell FILLED | apply (gone) | exactly what the venue confirmed |
| sell PARTIAL / PENDING | apply (remainder restored) | the rest of the lot still exists and can be exited |
| sell REJECTED | apply (restored) | the venue said no; the desk may try again, with a **new** key |
| sell UNKNOWN | apply (restored) + in flight | the socket died; the risk may be real, so count it and page |
| buy FILLED | apply | the confirmed lot, at the confirmed average price |
| buy REJECTED | apply (removed) | a phantom position must not hold a slot |
| buy PARTIAL / UNKNOWN | **keep the engine's larger position** | the remainder may still fill; over-counting risk is the safe direction |

`exit_in_flight` is refreshed from `ExecutionBook.unresolved_orders()` (sells only) on
every answer. While a contract is in that set:

| Path | Behaviour |
|---|---|
| a second close / trim | `close_while_exiting` / `trim_while_exiting` skip, nothing sent |
| an add | `add_while_exiting` skip - never increase exposure while leaving |
| the breakeven stop | silent no-op (it is polled every tick; a page per tick is noise) |
| the expiry cutoff | `HALT_AND_PAGE:exit_in_flight:assignment=...` - never a second sell |

**Realised P&L now follows the fill, not the post.** The engine books the whole intent at
the price in the Discord message. `Engine.align_realized` moves the engine onto the
confirmed fill (better or worse), reverses the P&L for a partial's unfilled remainder,
and reverses the whole assumption on a refused or unanswered exit - because no fill was
confirmed. That correction goes through `GovernorState.correct_result` (money and equity
path), not `record_result` (which would count a second trade), and it is idempotent under
a redelivered cumulative ack. Without it the daily stop would react to a number the venue
never printed: optimistic after a rejected sell, wrong after price improvement.

**The idempotency key is now the decision instant**, `contract:action:<ms>`. A counter
is not an identity: `trades_today` can be rewound by a confirmed-book sync, and
`trimmed` never advances on a refused trim, so a retry of a refused exit would reuse the
refused order's key and invite the venue to replay the refusal. Milliseconds of the
injected clock are unique in the process and across restarts, deterministic in replay,
and unique per *attempt* - so a retry is a new order and a redelivered answer is still a
duplicate.

```bash
python -m pytest tests/test_confirmed_exits.py -q     # 14 passed
```

Fourteen tests pin: a rejected sell restores the lot and a retry is a new order; a partial
leaves the remainder visible and blocks a second exit; the cutoff pages instead of selling
twice; an unknown sell restores the risk, pages, and never retries; a rejected buy frees
its slot; an unknown or partial buy keeps the larger position; a fill away from the post
moves realised P&L; a refused sell reverses the assumed profit; the correction is
idempotent; and a **restart** restores the in-flight guard from the book, not just the
position, so a second process cannot send a second sell. The 93-alert backtest, the 9/8 replay, and the paper broker are byte-identical
(`$57,951.00`, `$211.00`) because none of them attaches a confirmed book.

What this still does not do: there is no cancel/replace loop for a limit that rests
unfilled, so a partial whose remainder never trades stays in flight (visible, paged, and
settled by `recover`/`reconcile`) rather than being repriced automatically. The venue's
ack shape remains the credentialed unknown.

## Flatten: the exit no post was going to give you (`flatten.py`)

Every exit was event-shaped: a Discord post, a breakeven quote, or the 15:45 cutoff. The
moments a human wants to be flat **now** (the channel is compromised, a clip is
mis-sized, the world changed) are exactly the moments no post is coming. `killswitch`
stops entries and deliberately does not trap a position; there was still no command that
could leave one.

`python -m scorpion_mff.cli flatten` is that command, with three refusals that make it
safe enough to be the panic button:

| Rule | What it does |
|---|---|
| **the confirmed book is the list** | sells only lots the venue confirmed; it cannot create a position |
| **a price we can see, or nothing** | the limit is the live bid - never the posted price, the entry, the last bar, or a market order. An unpriceable lot is `UNPRICED` and stays on the book |
| **one exit per lot** | a contract with an unanswered sell is `IN_FLIGHT` and is never sold again |

Default is a **plan**: what it would sell, at what bid, with the estimated P&L, and
nothing is sent. `--live` rebuilds the close signal the live path expects for each lot
(from the contract key, so two calls on one ticker stay two lots) and places it through
`Listener.place` - the same intent row, venue answer, confirmed book, in-flight guard and
realised-P&L correction an exit post uses. `book.sync_engine` runs first, so a carried lot
whose contract already expired is **released, not sold**, and a lot from a previous
session is restored before it is priced.

The kill switch is **not** consulted. It stops buys; a desk that cannot flatten because it
stopped buying has the safety inverted. A rejected or unknown sell is named in the
report and paged; the lot stays confirmed and the engine's assumed profit is reversed by
the G19 join, so a failed panic does not look like a profitable one.

```bash
python -m scorpion_mff.cli flatten            # exit 0 flat / 1 a lot remains / 2 untrusted
python -m scorpion_mff.cli flatten --live     # needs MFF_RH_TOKEN + MFF_QUOTE_URL_TEMPLATE
python -m scorpion_mff.cli flatten --json     # the same report, machine-readable
```

Fourteen tests (`tests/test_flatten.py`) cover the plan having no side effects, the
unpriced and in-flight refusals, a live flatten ending flat with the confirmed P&L, a
second flatten being a no-op, a rejected sell restoring the lot and reversing the
assumed profit, the priced/unpriced split, an expired lot being released not sold, the
CLI's dry run/JSON, the token refusal, and a corrupt execution log.

Not built: a repricing ladder for a bid that moves away after the plan is printed (the
plan is a snapshot by definition), and any market-order fallback - both are desk
policies, not code decisions.

## Scheduled venue reconciliation: the feed (`venue.py`)

`reconcile.py` compares two books and `execution_book.py` owns one of them. Neither
answers "where does the *other* book come from, and can we believe it?" - which is why the
drift halt was manual. `scorpion_mff/venue.py` is the source, and its job is to refuse:

| Payload | Verdict | Why |
|---|---|---|
| `[{"contract_key": "...", "contracts": 3}]` | a book | the shape `reconcile` already speaks |
| `{"as_of": "...", "positions": [...]}` | a book, stamped | the venue's own time travels with the data |
| a duplicate contract key | **refused** | two answers to one question is not evidence |
| `positions: "lots"` / no list at all | **refused**, naming the keys it saw | "your path is wrong" must not read as "flat" |
| older than `--venue-max-age` (default 120s) | **refused** | an old book compared with a current one invents or hides drift |
| no timestamp, with an age limit set | **refused** | an undatable book is not evidence |
| 5xx, timeout, unconfigured | `VenueError` | no evidence - and never a flat book |

Two adapters: `FileVenueFeed` (an operator export or a fixture; the file's mtime is the
stamp when the payload has none) and `HttpVenueFeed` (a `GET` template plus a dotted
positions path, bearer token from `MFF_RH_TOKEN`, host-only labels so a token in a query
string cannot reach a log). `feed_from_env()` builds whichever the environment names, and
`arm` says out loud when neither is set.

The consequence lives where the halt already lives - `Engine.reconcile_positions`:

| Situation | What happens |
|---|---|
| drift | `halted`, `halt_reason=reconcile-drift`, one `alert` row (a page) |
| the same drift again on the next tick | a quiet `drift_unchanged` row, **no second page** |
| a *different* drift | a second page (it is new information) |
| clean, first check | one `reconcile:clean` row |
| clean after drift | `clean_after_drift` row, halt **stays** (recovery is an operator act) |
| feed unavailable / malformed / stale | `alert` row naming the cause, **no halt** |

That last row is a deliberate asymmetry. Drift is a disagreement between two readable
books, and it halts new buys. A broken adapter is not a disagreement, and halting on it
would turn an ops bug into a stopped desk - the human is already being paged. Exits remain
legal in every one of these states.

The local side is the **confirmed** book when one is attached. The engine deletes a
position at sell-*intent* time, so comparing the venue with the engine's decisions would
hide exactly the failure that matters: a sell that never arrived looks flat locally while
the venue still holds it. `reconcile --source auto` uses the confirmed log once it has
orders, falls back to the fill log before that, and prints which it used.

```bash
python -m scorpion_mff.cli reconcile --snapshot tests/fixtures/venue-positions.json
# exit 0 clean · 1 drift (with the next step printed) · 2 malformed, stale or unreadable
python -m scorpion_mff.cli arm --venue-snapshot live/venue-snapshot.json --venue-every 300
```

47 tests (`tests/test_venue_feed.py`) cover the taxonomy, the cadence, the de-duplicated
page, the no-halt-on-a-dead-feed rule, the confirmed-book comparison, and the HTTP adapter
against a real local server.

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

## Harvested from the orphan (`feat/risk-budget-context`)

The orphan branch carried a parser guard the live lane did not: a dollar amount followed
by option language is a contract term, not a fill. The live regex refused `$240 STRIKE`
but still read `NVDA STRIKE $180 CALLS CLOSED` as a **close at 180** — the same
phantom-profit class as the `19%` bug, one wording further out. Harvested here as the
wider lookahead (`STRIKE|CALLS|PUTS|EXP…|CONTRACT`), a skip when the dollar amount equals
the parsed strike, and four tests (`tests/test_parser.py`). The orphan is otherwise an
archive; the harvest map is `docs/fleet/ORPHAN.md`.

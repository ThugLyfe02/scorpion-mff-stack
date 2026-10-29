# Gems: the acceptance ledger

A **gem** here is not a feature list entry. It is a small, non-obvious invariant that
costs money when it is wrong, plus one command a reviewer can run to prove it still
holds. Every command below is offline, deterministic, and bounded to well under a
minute. `0` is the only passing exit code.

Run from the repo root with the project virtualenv active. On this Windows checkout
that is:

```bash
source .venv/Scripts/activate      # Git Bash; POSIX: . .venv/bin/activate
python -m pytest --version         # if this fails, use .venv/Scripts/python.exe
```

If the venv is not active, replace `python` with `.venv/Scripts/python.exe` (Windows)
or `.venv/bin/python` (POSIX). `scripts/gate.sh` resolves the venv itself and is the
single top-level check: `ruff -> mypy --strict -> pytest`, currently **736 passed**
(513 at the 3.a red-team, plus the three transport-quarantine tests added by final
integration that close O1, plus the offline Gateway lifecycle suite, plus the
confirmed-exit join and the operator flatten added by G19/G20).

## The one-line index

| Gem | What it refuses to guess | Acceptance command |
|---|---|---|
| G1 | the market calendar | `python -m pytest tests/test_calendar.py -q` |
| G2 | an expired slot is never silently free | `python -m pytest tests/test_expiry_engine.py -q` |
| G3 | the close is a clock, not a post | `python -m pytest tests/test_expiry_engine.py -q -k "cutoff or tick"` |
| G4 | an assignment projection from premium alone | `python -m scorpion_mff.cli expiry audit` |
| G5 | one half-open probe, and exits are ungated | `python -m pytest tests/test_execution.py -q -k breaker` |
| G6 | a spread it cannot price | `python -m pytest tests/test_execution.py -q -k spread` |
| G7 | an order's terminal without a venue answer | `python -m pytest tests/test_ledger.py -q` |
| G8 | recovery by retry | `python -m pytest tests/test_ledger.py -q -k recover` |
| G9 | the 93-alert corpus as a moving target | `python -m scorpion_mff.cli backtest` |
| G10 | a replay that is not the live path | `python -m scorpion_mff.cli replay tapes/sep8.json` |
| G11 | an input the invariants do not cover | `python -m scorpion_mff.cli fuzz --iterations 5000 --seed 7` |
| G12 | a switch you cannot see before arming | `python -m scorpion_mff.cli doctor` |
| G13 | a reconnect that silently drops the session | `python -m pytest tests/test_gateway.py -q` |
| G14 | a daily loss limit that only fires after the loss | `python -m pytest tests/test_risk_budget.py -q` |
| G15 | an add that bypasses the stand-down or the governor | `python -m pytest tests/test_add_safety.py -q` |
| G16 | a book that disagrees with the venue and does not say so | `python -m pytest tests/test_reconcile.py -q` |
| G17 | a confirmed fill that a restart, a duplicate or a partial can corrupt | `python -m pytest tests/test_execution_book.py -q` |
| G18 | a venue book that is stale, undatable, or not a book at all | `python -m pytest tests/test_venue_feed.py -q` |
| G19 | a running bot that thinks it exited because the venue has not answered | `python -m pytest tests/test_confirmed_exits.py -q` |
| G20 | an operator with no way to get flat that does not invent a price or sell twice | `python -m pytest tests/test_flatten.py -q` |

Each gem follows the same shape: **what it is**, **the acceptance command**, **the
invariant**, **the failure mode it buys down**, and the **3.a red-team record** for it.
The 3.a quotes are verbatim; line numbers are as 3.a reported them, so function and
module names are the stable reference.

---

## G1. The calendar refuses to guess a year

`scorpion_mff/calendar.py` — the ET clock, the session, the half-day close, and the
DST seam. It is deliberately not a full exchange calendar: it knows 2025 and 2026 and
raises `CalendarError` for anything else.

**Acceptance (one command):** `python -m pytest tests/test_calendar.py -q` -> 48 passed.

**Invariant:** every lookup on an unknown year raises before it can return `False`;
a naive datetime is refused; the DST offset is chosen by the UTC *instant* at the 2 a.m.
seam; the half-day close is 13:00 ET; and `et_date` at 01:30 UTC returns the previous
New York date, not the UTC date.

**Failure mode it buys down:** a guessed close turns a same-day contract into an
overnight hold at exactly the minute the market stops showing a bid; a per-date DST
approximation shifts every join by an hour in March and November; a UTC day comparison
closes a 2025-09-09 contract a day early.

**3.a red-team record, verbatim:**

> **PASS — PR#1 `sessions.py` → live `calendar.py` (semantics + fail-closed).** Holiday/half-day tables, `OPEN_ET`/`CLOSE_ET`/`HALF_DAY_CLOSE_ET`, and `et_offset` DST rule are identical (`.worktrees/pr1/src/scorpion/sessions.py:16-62` vs `scorpion_mff/calendar.py:30-90`). Live hardens two things: `is_trading_day` checks the year *before* the weekend so an unknown-year Saturday raises rather than silently returning `False` (`calendar.py:200-210`), and `et_date`/`et_offset_at` date the DST seam by instant (`calendar.py:96-127`) where PR#1 used a per-date approximation (`sessions.py:58-65`). Both are strengthenings, not divergences.

---

## G2. A released slot is never a silent slot

`Engine._expire_carried` in `scorpion_mff/engine.py`. When a carried contract has
provably expired, it stops counting against `MAX_CONCURRENT`; when the expiry cannot be
read, it keeps the slot and says why.

**Acceptance (one command):** `python -m pytest tests/test_expiry_engine.py -q` -> 38 passed.

**Invariant:** release only when `same_day` is true or the ISO expiry is strictly before
the ET trading date. An unparseable expiry (`Dec 5`) is **not** released. Every release
writes `expiry_unresolved:<why>:<expiry>:assignment=<shares|unknown>` with
`accepted=False`, so an unmarked expiry never enters realised P&L and never leaves the
log silent.

**Failure mode it buys down:** one alert with no recorded exit holds both slots forever
and the hone goes silent mid-month; freeing a live slot on a guessed expiry under-counts
risk on the next entry; counting the release as P&L invents money.

**3.a red-team record, verbatim:**

> **PASS — PR#1 `expiry.py` release-on-expiry → live `engine._expire_carried`.** PR#1 releases only `expiry < market_day` and never guesses unparseable keys (`expiry.py:96-135`); live `_expire_carried` (`engine.py:329-376`) preserves both (`released = same_day or (iso is not None and iso < day)`, and the `elif iso is None` branch keeps the slot with an `expiry_unparsed` alert). The live version adds the 0DTE-token release and the mandatory `expiry_unresolved:…:assignment=` alert — a superset, not a rewrite. Note: live `scorpion_mff/expiry.py` is a *different* feature (the 15-minute cutoff), not a port of PR#1 `expiry.py`.

---

## G3. The close is a clock, not a post

`Engine.expiry_intents` in `scorpion_mff/engine.py`, driven by `Listener.tick` in
`scorpion_mff/listener.py`. A 0DTE is flattened at the bid actually seen inside the
cutoff window, or paged and left on the book when there is no live bid.

**Acceptance (one command):**
`python -m pytest tests/test_expiry_engine.py -q -k "cutoff or tick"` -> 12 passed.

**Invariant:** 15:44 ET is `HOLD`; 15:45 is `FORCE_CLOSE` at the observed bid; 16:01 is
`HALT_AND_PAGE` even with a bid; a missing, zero, negative, or NaN bid is
`HALT_AND_PAGE` and places nothing; a half-day cutoff is 12:45, not 15:45. The tick is
`asyncio.shield`ed so firing the clock cannot cancel the in-flight Gateway fetch and
swallow the next message.

**Failure mode it buys down:** a 0DTE held through the bell comes back as 100 shares per
contract; flattening at an invented price books a fill the market never offered; a quiet
channel never fires because no Discord post arrives to trigger a decision.

**3.a red-team record, verbatim:**

> **PASS — 0DTE boundary matrix.** Independent engine run (strike 700, long call, entry 1.05): 15:44+bid → **HOLD** (no intents, open=1); 15:45+bid → **FORCE_CLOSE** (`sell` 1× @2.00, open=0); 16:01+bid → **HALT_AND_PAGE** `at_or_after_close` (open=1); 15:45 no-bid → **HALT_AND_PAGE** `cutoff_without_live_bid` (open=1). Underlying spot changed **only the page evidence, never the action**: no-bid page ends `assignment=+100` (ITM spot 710) / `+0` (OTM 690) / `unknown` (no spot). Matches `expiry.verdict_detail` ordering (`expiry.py:257-320`).

---

## G4. The expiry audit is allowed to refuse

`python -m scorpion_mff.cli expiry audit` (`cli.expiry_audit`). It measures the corpus
against the cutoff and prints the number it cannot support as **REFUSED** rather than
filling it in.

**Acceptance (one command):** `python -m scorpion_mff.cli expiry audit` -> exit 0 (the
refusal is a printed result, not a crash), with:

```
same-day expiry          40 / 93  (43.0%)
no explicit final close  65 / 93  (69.9%)   floor 61 from the outcomes
cutoff force-close        0 / 40 same-day
   no recorded exit time  10 / 40 - at the cutoff these PAGE
assignment projection   REFUSED - the corpus records the option premium only
clock caveat: the corpus carries no timezone proof
```

**Invariant:** the audit refuses assignment shares when the corpus has no underlying
close at expiry, and refuses to force-close a trade with no recorded exit time (it pages
instead). A missing price is not an out-of-the-money contract.

**Failure mode it buys down:** projecting an assignment from premium alone fabricates
the exact risk number the audit exists to size; force-closing on an invented exit time
changes live behaviour without evidence and then calls the change "measured".

**3.a refusal, verbatim:** `assignment REFUSED`; and from the audit's own output:
> assignment projection   REFUSED - the corpus records the option premium only: it has no underlying close at expiry, so assignment shares cannot be projected. A missing price is not an out-of-the-money contract

---

## G5. One half-open probe, and exits are never gated

`execution.CircuitBreaker` in `scorpion_mff/execution.py`, consulted only for buys by
`Listener._place`. This is the PR#1 `outbox.Breaker` idea, tightened.

**Acceptance (one command):** `python -m pytest tests/test_execution.py -q -k breaker` -> 6 passed.

**Invariant:** `failure_threshold` consecutive transport failures open the breaker for
`cooldown_seconds`; after the cooldown exactly **one** probe is admitted; a success
closes; a failed probe re-opens for a fresh cooldown. Only `intent.action == "buy"` asks
the breaker. A business reject is the venue answering, so it resets the failure streak
instead of tripping the breaker.

**Failure mode it buys down:** a dead venue plus a live listener hammers orders; PR#1's
`allow()` cleared `opened_until` after the first probe and admitted unbounded probes
until one failed; gating exits would trap a position in the outage the kill switch
exists to escape.

**3.a red-team record, verbatim:**

> **PASS — PR#1 `outbox.py` `Breaker` → live `execution.CircuitBreaker`.** Threshold/cooldown/one-probe/success-closes/failure-reopens all hold. Live is *stricter*: PR#1's `allow()` flips `opened_until=None` after the first post-cooldown probe and thus admits unbounded probes until one fails (`outbox.py:78-84`); live tracks explicit `BreakerState` + `_probe_armed` and admits **exactly one** probe (`execution.py:44-68`). Independent run confirmed: 2nd `allow()` at half-open returns `False`.

> **PASS — breaker open/half-open with an exit attempt (kill-switch invariant).** `_place` gates only `intent.action == "buy"` (`listener.py:147`). Open breaker: buy → `reject` reason `breaker_open`, exit → broker ack. Independent run: `broker.acks == 1` (the sell) with the buy refused; the successful exit closes the breaker via `record_success`.

---

## G6. The spread gate fails closed and is off by default

`execution.spread_ok` in `scorpion_mff/execution.py`. The gate is `None` unless
`MFF_MAX_SPREAD_RATIO` arms it.

**Acceptance (one command):** `python -m pytest tests/test_execution.py -q -k spread` -> 5 passed.

**Invariant:** `max_ratio=None` passes every quote; when armed, an unpriceable bid
(`bid <= 0`) returns `False`; exactly the boundary is inside the gate; the 93-alert
backtest and the 9/8 replay keep their original decisions because the gate is off.

**Failure mode it buys down:** paying a spread wider than the edge can recover; or
arming a "safety" default that silently changes the corpus and makes a regression
indistinguishable from a guard.

**3.a red-team record, verbatim:**

> **PASS — PR#1 `guard.py` fail-closed philosophy → live spread gate.** "UNEVALUABLE blocks" (`guard.py:44-45`) is reflected in `spread_ok` failing closed on an unpriceable bid (`execution.py:106-122`). Verified: `spread_ok(Quote(0.0,1.0), 0.10)` → `False`; `max_ratio=None` → always pass; exact boundary `0.10` inside. **The actual `guard.py` check set (`evaluate`, `DEFAULT_CHECKS`: drift/vol/tail/burst/cooldown) was NOT ported** — no `scorpion_mff` module defines them. No report claimed it was, so this is an observation, not a failed claim.

The last sentence is a **deliberate unported surface**, recorded under "Known unported
surfaces" below. It is not a missing half of the spread gate.

---

## G7. Intent before place; the ledger never guesses

`Listener._place` writes the `intent` row before the broker call; `ledger.Ledger`
matches it to a later terminal; `broker.OrderLookup` is three-valued. Together they make
a crash mid-order visible instead of silent.

**Acceptance (one command):** `python -m pytest tests/test_ledger.py -q` -> 21 passed.

**Invariant:** the intent row and its idempotency key exist before any network call; a
terminal resolves an intent only when its sequence number is strictly later; `SEEN`,
`NOT_SEEN`, and `CANNOT_SAY` are the only lookup answers; a pre-ledger log with no
intents still verifies clean; replay decisions never leak ledger bookkeeping. **A
transport failure is never a terminal:** it writes a `transport_unknown` quarantine row,
the intent stays unresolved, `verify` names the venue ambiguity, and `recover` settles it
from a lookup. A transport-failed exit also pages immediately, because the engine books
flat at intent time while the venue may still hold the position.

**Failure mode it buys down:** a crash between intent and ack loses the order, or a
restart double-places it; a local book that looks flat while the venue still holds risk;
a recovery routine that resolves an ambiguity by guessing; a dead socket erased as a
`reject`, which would hide exactly the unconfirmed exits that need reconciliation.

**3.a red-team record, verbatim:**

> **PASS — PR#1 `reconciliation.py` tri-state → live `broker.OrderLookup` + `ledger.Ledger`.** `seen`/`not_seen`/`unknown` (`reconciliation.py:150-166`) maps 1:1 to `SEEN`/`NOT_SEEN`/`CANNOT_SAY` (`broker.py:37-42`); `_interpret_lookup` (`broker.py:235-253`) never guesses (`404`→NOT_SEEN, non-2xx→CANNOT_SAY, ambiguous 2xx body→CANNOT_SAY, no template→CANNOT_SAY). **The book-vs-venue drift-halt (`reconcile`, `reconcile_and_halt`, `phantom_local`/`missing_local`/`quantity_mismatch`) was NOT ported** — no `scorpion_mff` module defines it. Again: not claimed, so observation only.

> **PASS — crash mid-place leaves `intent` with no terminal.** `listener._place` writes `intent` **before** the broker call (`listener.py:131-147`, `fills.record(event="intent", …, note=key)` precedes `self.broker.place(intent)`). Independent run: a broker that raises mid-place leaves exactly `['intent']`, and `Ledger.unresolved` returns that intent (`ledger.py:38-65`). `verify` exits 1 on an intent-only log.

---

## G8. Recovery is a lookup, never a retry

`python -m scorpion_mff.cli recover` (`cli.cmd_recover`). It may only ask the broker what
happened to each unresolved idempotency key and append the answer.

**Acceptance (one command):** `python -m pytest tests/test_ledger.py -q -k recover` -> 3 passed.

**Invariant:** `SEEN -> recovered`, `NOT_SEEN -> not_sent`, `CANNOT_SAY -> still
unresolved` with exit 1 and no row appended. `recover` never calls `place`; the test's
stub raises `AssertionError` if it ever does (`stub.placed == 0` in every case). A
transport-quarantined intent is recoverable like any other unresolved intent: `verify`
names it (`[transport unknown: ...]`), and `recover` settles it without a retry.

**Failure mode it buys down:** a recovery command that retries can double-send; one that
assumes "not sent" can drop an order that is live at the venue; one that appends a
terminal from an ambiguous lookup turns an unknown into a lie.

**3.a red-team record, verbatim:**

> **PASS — lookup tri-state, all three branches.** Independent run produced `not_seen` (unplaced), `seen` (placed / 200 `found:true`), `not_seen` (404 / `found:false`), `cannot_say` (no template / transport error / `{"status":"unknown"}`). `recover` maps SEEN→`recovered`, NOT_SEEN→`not_sent`, CANNOT_SAY→still unresolved (exit 1) and **never places** (`placed == 0` in every case).

---

## G9. The corpus is a pin, not a promise

`python -m scorpion_mff.cli backtest` over all 93 recorded alerts. It is the regression
oracle: every new guard must prove it does not move these numbers unless the move is
explicitly chosen and documented.

**Acceptance (one command):** `python -m scorpion_mff.cli backtest` -> exit 0, with:

```
alerts        93 over 59 days
taken         91   skipped 2   rejected 0
contracts     405   notional $67,400
P&L           $57,951.00   avg/trade $636.82   win rate 90.9%  (80W/8L)
skips / rejects:
    2  ignored:ticker_excluded
```

**Invariant:** the corpus decision set is byte-identical across the PR#1 ports. The
spread gate, expiry guard, and execution safety additions are opt-in on this path, and
the two repeated contract keys are disclosed rather than silently deduplicated.

**Failure mode it buys down:** a "safety" change flips a skip or an exit, the P&L moves,
and no one can tell whether that is a fix or a regression because the baseline was never
pinned.

**3.a record, verbatim:** "Backtest and replay are byte-identical to the pinned baseline
in report 2.a."

---

## G10. A replay that is the live path

`python -m scorpion_mff.cli replay tapes/sep8.json` runs the same parser -> engine ->
pricing -> sizing pipeline as the listener, over the recorded 9/8 tape, with measured
latency.

**Acceptance (one command):** `python -m scorpion_mff.cli replay tapes/sep8.json` -> exit 0, with:

```
events 4 | buys 2 (6 contracts) | sells 2 | realised $211.00
latency worst 0ms  budget 10000ms  breaches 0
buy   1x QQQ|CALL|719|0DTE         @ 1.05
sell  1x QQQ|CALL|719|0DTE         @ 1.11
buy   5x NVDA|PUT|227.5|2026-09-11 @ 2.72
sell  5x NVDA|PUT|227.5|2026-09-11 @ 3.13
```

**Invariant:** replay and live share the decision code, not a simulation of it; the
default path leaves the new guards off (`spread gate`, `enforce_expiry`,
`enforce_breakeven`), so the replay is parity evidence and not a second implementation.

**Failure mode it buys down:** the proof drifts from the product. A replay that no
longer reproduces the post-mortem cannot tell you whether the live lane still avoids the
9/8 failure.

**3.a record, verbatim:** "Backtest and replay are byte-identical to the pinned baseline
in report 2.a."

---

## G11. Fuzz: ten invariants, deterministic

`python -m scorpion_mff.cli fuzz` throws generated channel-shaped posts at the real
parser and engine and checks ten invariants after every message.

**Acceptance (one command):** `python -m scorpion_mff.cli fuzz --iterations 5000 --seed 7`
-> exit 0, `CLEAN`, every invariant held.

**Invariant:** no sale without a holding; a percentage never becomes a price; no option
priced above its strike; the book never exceeds `max_concurrent`; every intent carries a
reason; a kill switch blocks buys but never exits; same input -> same output; `qty >= 1`;
a closed contract is not re-entered same-day; the parser never raises.

**Failure mode it buys down:** the `%`-as-price defect sold a $3.13 contract for $19
($8,146 of phantom profit). The campaign also found that a named exit for an unheld
contract could flatten a different held position, and that gating exits during a
kill-switch would trap risk. A fuzzer with wrong invariants is a noise generator, so the
invariants are themselves tested.

**3.a record, verbatim:** **CLEAN**, all 10 invariants held.

---

## G12. Every switch is visible before arming

`python -m scorpion_mff.cli doctor` prints channels, tokens (masked), broker mode, kill
switch, and the spread-gate setting. `arm` refuses on a missing token, a missing or
stale risk policy, a corrupt governor file, or a live run with no quote feed.

**Acceptance (one command):** `python -m scorpion_mff.cli doctor` -> exit 0, containing:

```
kill switch                 ARMED (no file)
discord token               MISSING (arm will refuse)
broker mode                 DRY (no orders leave the box)
spread gate                 OFF (MFF_MAX_SPREAD_RATIO unset)
```

**Invariant:** an unset safety switch is printed as `OFF`, not omitted; "dry" and "live"
are distinct and explicit; the token is masked. The default is dry with the spread gate
off; arming is a separate, reviewed act.

**Failure mode it buys down:** arming while believing a gate is on (or off) when it is
the opposite; a live run that was meant to be dry; a hidden default that only appears in
the fill log after it has already decided.

---

## G13. A reconnect is a session, not a restart

`scorpion_mff/gateway.py` is the Discord session state machine; `listener.WebsocketGateway`
is the socket loop that consumes it. A dropped websocket no longer ends the pump: the
transport reconnects with the prior `session_id` and the last dispatched sequence (RESUME),
so alerts posted during the gap are replayed instead of being lost.

**Acceptance (one command):** `python -m pytest tests/test_gateway.py -q` -> 20 passed.

**Invariant:** every DISPATCH sequence is tracked and the heartbeat carries the last one; an
unACKed heartbeat is a zombie connection and reconnects; op 7 and a resumable op 9 keep the
session; 4007/4009 forget it and re-IDENTIFY; 4004/4010-4014 stop and page instead of
looping; a sequence gap is paged, never smoothed over; a redelivered sequence is dropped
before the parser sees it. Routine reconnects write a `gateway_reconnect` row; a gap,
zombie or fatal close writes an **alert** row that `watch --once` already pages on.

**Failure mode it buys down:** a routine websocket close ends the async generator, the pump
exits, the heartbeat stops, and the hone is silent while the market keeps moving - the 9/8
failure in transport form. A re-IDENTIFY after every close drops every alert buffered during
the reconnect. A zombie socket looks alive and delivers nothing. A missed sequence is a
missed exit, not a counter.

**Not a port:** PR#1 and PR#3 use `discord.py`, which owns this lifecycle for them. This
state machine exists because the live lane hand-rolls the Gateway to keep the standard
library as its only runtime dependency.

---

## G14. The loss is priced before the order, not after it

`scorpion_mff/risk.py` + `Engine.risk_budget`. The governor is a **realised** risk layer:
it cannot stop a first loss that has not happened. Two full clips can therefore realise
twice the daily loss limit. The pre-trade budget closes that hole by admitting a **contract
count** whose premium keeps the worst-case day inside the limit:

```
worst_case = realised_today - open_premium - new_premium
```

**Acceptance (one command):** `python -m pytest tests/test_risk_budget.py -q` -> 24 passed.

**Invariant:** every cap (total premium, per underlying, declared correlation cluster, daily
worst case) can only **shrink** the locked clip; an unmapped symbol is its own cluster; a
symbol in two clusters is a refused configuration; a budget refusal is a `skip` row naming
the cap, never silence; the same pass runs on an **add** against the position already held;
and an absent budget leaves every decision byte-identical.

**Failure mode it buys down:** the daily stop is an accounting fact after the money is gone.
A book that can hold two $3,000 clips has a $6,000 worst case against a $3,000 limit, and
the governor only learns about it after both expire. The budget is the only component that
can refuse the *second* clip before the first loss exists.

**Operator surface:** `python -m scorpion_mff.cli risk validate` parses every cap;
`risk show` rebuilds the book from the fill log and prints the worst-case day and headroom
by underlying and cluster; `arm` loads `live/risk-budget.json` when present and refuses an
unreadable file rather than trading uncapped.

## G15. An add is a new buy, not a footnote to the first one

`Engine._on_add`. The add path checked the kill switch but not the engine's
`halted` flag, and it never consulted the governor. Two failures followed:

```
STAND DOWN                     -> entry rejected
adding at 1.30                 -> position.contracts += qty   (exposure grew)

daily loss limit reached       -> entry shrunk
the next "adding" post         -> full add clip             (stop undone)
```

**Acceptance (one command):** `python -m pytest tests/test_add_safety.py -q` -> 5 passed.

**Invariant:** an add runs the same `halted` check and the same
`governor -> apply_contracts` ladder as the entry, before the position is mutated;
a stand-down rejects the add with `halted:stand-down`; a governor stand-down skips
with `governor:daily_stop`; a half-size policy halves the add clip (22 -> 11) and the
intent names the governor; and **exits remain legal** in every one of those states,
because a switch that traps a position is worse than no switch.

**Failure mode it buys down:** a risk layer with a hole in it. The daily stop was
enforced on the way in but not on the way up, so the one post that increases the
position could undo the stop that was supposed to prevent it. The same shape as the
9/8 miss: not a wrong decision, a decision that never ran.

**Regression note:** the add path is now a single ladder - halt, kill switch, add
budget, governor, pre-trade budget, mutation - so a future check added to the entry
has one obvious counterpart to add here.

## G16. A book that disagrees with the venue must say so

`scorpion_mff/reconcile.py` + `Engine.reconcile_positions`. The engine's positions
are a decision cache; the venue holds the contracts. Nothing compared them, so a
restart, a rejected buy, a partial fill or a manual trade could leave the bot flat in
its own memory while the venue still held risk (or the reverse).

**Acceptance (one command):** `python -m pytest tests/test_reconcile.py -q` -> 20 passed.

**Invariant:** the comparator names all three drift directions in one report -
`phantom_local` (we hold it, the venue does not), `missing_local` (the venue holds it,
we do not), `quantity_mismatch` (both, different counts). A duplicate venue line, a
negative/boolean/float count, or a line without a contract key is **refused**, not
summed or clamped. A clean snapshot writes one `reconcile` row; drift sets
`halt_reason=reconcile-drift`, writes an alert, and rejects new buys with
`halted:reconcile-drift` - while an exit still runs. The operator surface is
`reconcile --snapshot SNAP`: exit 0 clean, 1 drift (page), 2 malformed snapshot.

**Failure mode it buys down:** the bot believes it is flat while the venue holds a
position, so no exit will ever be posted for it; or the bot believes it holds a
contract it cannot sell, so P&L and the governor are fiction. Both are silent, which
is the exact class of failure this package exists to remove.

**What is not built:** the venue *feed*. The comparison is built and the snapshot
format is documented; a positions endpoint (or an operator upload) is the remaining
integration, and the CLI is honest about it (`--snapshot` is required). The gate stays
closed rather than inventing a snapshot from the fill log, which would compare the
book with itself and always pass.

---

## G17. A confirmed fill survives a restart, a duplicate and a partial

`scorpion_mff/execution_book.py` + the `book` command group. The fill log records
decisions, `ledger.py` matches an intent to a terminal, and `reconcile.py` compares two
books - but none of them is durable **state**. So four defects were structurally possible:
a rejected buy holding a concurrency slot forever, a partial fill booked as the full clip
(overstating realised P&L and the equity path), a rejected sell that had already moved the
drawdown, and a restart that forgot every open position.

**Acceptance (one command):** `python -m pytest tests/test_execution_book.py -q`
-> 66 passed.

**Invariant:** an ack carries a **cumulative** quantity, so only the delta is applied and a
3-then-2 split realises exactly what one 5-lot realises ($250 on a 1.00 -> 1.50 move).
Repeating the same ack applies nothing and counts as a duplicate; a *regressing* ack is
refused, never clamped; a reject is terminal and a late reject cannot undo a fill; a sell
larger than the lot is clamped and counted; a sell with no lot realises nothing (this book
is long premium only, so an unheld sell is an anomaly, not a short); a fill for an order
this book never recorded is refused (adopting an unseen execution is an operator act). A
transport failure is `unknown`, never a terminal, so the order stays unresolved until
`recover` settles it from a venue lookup. A torn **final** append is ignored and counted; a
torn middle line is refused. `sync_engine` restores the engine as `baseline + confirmed
events`, so the drawdown, the peak equity path, the closed contracts and the pipe-test
counter all survive a restart - and a lot carried past expiry is released first, marked at
zero and flagged `assumed_zero`.

**Operator surface (all offline):**

```bash
python -m scorpion_mff.cli book show                          # the book, digest, anomalies
python -m scorpion_mff.cli book replay                        # replay twice; exit 1 on divergence
python -m scorpion_mff.cli book verify [--strict]             # defects; --strict fails on unresolved
python -m scorpion_mff.cli book sync                          # restore a fresh engine from the log
python -m scorpion_mff.cli book export                        # confirmed fills -> tca executions
```

`arm` refuses to start when any order has no venue answer (override: `--allow-unresolved`),
because arming on a hypothesis is how a restart becomes a double entry.

**Failure mode it buys down:** two truths about what is held. The engine's position book is
a decision cache; the venue's answer is the truth; without a durable middle the two drift
apart silently and the drift is discovered by a rejected exit at the worst moment.

**What is not built:** the live venue's ack shape is still unconfirmed, so the book is
proven offline only. `book export` exists so the first real ack prices itself through
`tca fills` rather than being typed in by hand.

---

## G18. A venue book that is stale, undatable, or not a book must be refused

`scorpion_mff/venue.py` + the scheduled reconcile in `Listener.tick`. `reconcile.py`
compares two books and `execution_book.py` owns one of them; neither answered *where the
other book comes from and whether it can be believed*. That gap is why the drift halt was
manual, and it is the difference between "the venue says X" and "a file someone left on
disk says X".

**Acceptance (one command):** `python -m pytest tests/test_venue_feed.py -q` -> 47 passed.

**Invariant:** a snapshot is evidence only if it is (a) a book, (b) dated, and (c) not too
old. A bare array or `{"as_of": ..., "positions": [...]}` is accepted; a duplicate contract
key, a non-array position list, a payload with no recognisable list, a negative/boolean/float
count, a missing key, an unreadable file, a non-2xx answer and a transport failure are all
**refused** - never an empty book, because "no positions" would silently stop every exit. A
snapshot older than the limit, or one that cannot be dated while a limit is set, is refused
with its age. The HTTP adapter's error strings and `source` label never carry the token.

**The consequence is separate from the evidence.** Drift between two readable books halts
**new buys** and pages **once** (a repeated identical drift writes a quiet
`drift_unchanged` row, not a second page; a changed drift pages again). A clean re-check
after drift is recorded as `clean_after_drift` and does **not** clear the halt: `halted` is
shared with STOP/stand-down and lifting it is an operator act. A feed that is dead,
malformed or stale pages and halts **nothing** - a broken adapter must not become a stopped
desk - and exits stay legal in every state.

**Failure mode it buys down:** a book that disagrees with the venue and does not say so
(`phantom_local` / `missing_local` / `quantity_mismatch`), and its mirror image: a *healthy*
desk halted because an adapter returned 503 or an operator compared a snapshot from
yesterday. Both are silent in opposite directions; the taxonomy makes each one a named row.

**Operator surface:**

```bash
python -m scorpion_mff.cli reconcile --snapshot tests/fixtures/venue-positions.json
python -m scorpion_mff.cli reconcile --snapshot SNAP --source confirmed --max-age 120
python -m scorpion_mff.cli arm --venue-snapshot live/venue-snapshot.json --venue-every 300
```

`reconcile` exits 0 clean / 1 drift / 2 malformed-stale-unreadable, and prints the *source*
of the local book it used (`confirmed` when the execution log has orders, else `fills`).

**What is not built:** the real endpoint. `MFF_VENUE_URL` + `MFF_VENUE_POSITIONS_PATH` +
`MFF_VENUE_TIME_PATH` are yours to confirm; this stack does not invent a vendor schema, and
the file feed plus the fixture path are what is proven offline.

---

## G19. A running bot must not believe it exited before the venue did

`scorpion_mff/listener.py` (the four `_book_*` joins) + `Engine.exit_in_flight` +
`Engine.align_realized` + `GovernorState.correct_result`. `execution_book.py` made a
**restart** honest (`sync_engine`). The process that keeps running still had the older
hole: the engine deletes a position and books P&L at **sell-intent** time, so a sell that
was rejected, partially filled or never answered left the bot flat, with the contract in
`closed` and "never chase" refusing to exit again - while the venue still held the risk.

**Acceptance (one command):** `python -m pytest tests/test_confirmed_exits.py -q` ->
14 passed.

**Invariant:** after every venue answer, the engine's position for that contract is the
confirmed book's position, and `exit_in_flight` is exactly the set of contracts with a
non-terminal sell at the venue. The two directions are deliberately asymmetric:

* a sell that is FILLED/PARTIAL/REJECTED/UNKNOWN is applied - a refusal restores the lot,
  and an unknown restores it **and** keeps it in flight (the risk may be real);
* a buy is applied only when FILLED or REJECTED - a partial or unanswered buy keeps the
  engine's larger position, because the remainder can still fill and over-counting risk
  is the safe direction.

While a contract is in flight, a second close/trim (`*_while_exiting` skip), an add
(`add_while_exiting`) and the breakeven stop (silent no-op) are refused, and the expiry
cutoff writes `HALT_AND_PAGE:exit_in_flight` instead of sending a second sell. Realised
P&L is moved onto the confirmed fill (`confirmed - already_applied`, idempotent under a
redelivered ack); a partial reverses the P&L for the unfilled remainder, and a refused or
unknown exit reverses the whole assumption. The correction goes through
`correct_result`, so the trade counter and loss streak do not advance twice.

The **restart** path is in the same invariant: `sync_engine` now restores
`exit_in_flight` from the book's unresolved sells (`SyncReport.in_flight_sells`), so a
process that arms with a sell the venue never answered cannot send a second one. Without
that, the guard existed only in the process that sent the first sell - the one process a
restart is not.

**Failure mode it buys down:** the quiet asymmetry that costs the most money - the
engine is flat (and locked out of exiting) while the venue holds a lot, or the engine
books a profit on a sell the venue refused and the daily stop never sees the loss. Also
its mirror: a refused buy holding a concurrency slot forever, and a partial buy reported
as a full clip.

**The idempotency key is the decision instant** (`contract:action:<ms>`), not a counter.
`trades_today` can be rewound by a confirmed-book sync and `trimmed` never advances on a
refused trim, so a retry of a refused exit would reuse the refused order's key. A retry
is a new order; a redelivered answer is still a duplicate.

**What is not built:** no cancel/replace loop, so a partial whose remainder never trades
stays visibly in flight (paged, settled by `recover`/`reconcile`) rather than being
repriced automatically. The venue's ack shape is still the credentialed unknown.

---

## G20. The operator's exit: confirmed lots, seen bids, one sell each

`scorpion_mff/flatten.py` + `python -m scorpion_mff.cli flatten`. Every exit was
event-shaped - a Discord post, a breakeven quote, the 15:45 cutoff. There was no way for a
human to say "flat now", which is precisely the moment (`killswitch` stops buys but must
not trap a position) the command exists for.

**Acceptance (one command):** `python -m pytest tests/test_flatten.py -q` -> 14 passed.

**Invariant:** the command sells the **confirmed** book only, each lot at the **live bid**
(never the posted price, the entry, the last bar, or a market order), and **never sells a
lot twice**: a contract with an unanswered sell is `IN_FLIGHT` and skipped. An unpriceable
lot is `UNPRICED` and stays on the book; the report names it and the command exits 1. A
carried lot whose contract already expired is **released by `sync_engine`, not sold**. The
default is a dry run that touches nothing; `--live` rebuilds the close signal for each lot
from its contract key and places it through `Listener.place`, so the intent row, venue
answer, confirmed book, in-flight guard and realised-P&L correction are the same code an
exit post uses. The kill switch is deliberately **not** consulted: it stops entries, and a
desk that cannot flatten because it stopped buying has the safety inverted.

**Failure mode it buys down:** the only remaining way to lose control of a live position -
no human command can leave it, or the command leaves it twice / at an invented price. It
also makes a *failed* panic honest: a rejected sell restores the lot, pages, and reverses
the profit the engine assumed (G19), so a failed exit never looks like a profitable one.

**Exit codes:** 0 the confirmed book is flat (or a plan covers every lot), 1 a lot remains
or could not be priced/sold, 2 the execution log or quote feed cannot be trusted.

**What is not built:** a repricing ladder for a bid that moves after the plan is printed
(the plan is a snapshot), and any market-order fallback. Both are desk policies, not code
decisions.

---

## What we deliberately do not build

These are not backlog items with an ETA. They are gates that would have to open before the
component would be honest, and until then the refusal is the feature.

| Not built | Why the gate is closed | What exists instead |
|---|---|---|
| **No third parser** | Two parsers for one Discord vocabulary is two sources of truth. The live lane has exactly one deterministic parser and one engine; PR#1's control plane is never imported. A new shape is a change to `parser.py` plus fixtures, not a parallel grammar. | `scorpion_mff/parser.py` + `tests/test_parser.py`, `tests/test_contract_anchor.py`, and the `fuzz` corpus. |
| **No conformal sizing on 93 trades** | 93 alerts over 59 sessions, 40 same-day, only 30 with a recorded exit time, clustered by day and ticker. Exchangeability is not credible at that size, so a conformal interval would be numerically valid and semantically empty. A coverage claim needs a live sample across regimes, a pre-registered nonconformity score, and a held-out period. | Expectancy with confidence intervals and shrinkage (`desk`), whose uncertainty is disclosed in `POLICY.md`; no per-trade coverage guarantee is claimed. |
| **No latency-to-edge threshold without measured alert-to-fill drift** | Latency is measured (`replay ... worst 0ms, budget 10000ms, breaches 0`), but there is no measured **alert-to-fill drift** linking a delay to a worse price or a missed edge. Choosing a threshold before that measurement is choosing a number, not a policy. The gate is paired `MESSAGE_CREATE -> intent -> ack -> fill` timestamps and realised edge per event; the threshold must be derived from that distribution. | `LatencyBudget`, per-event latency rows, and the fill log. The budget is a ceiling, not an edge claim. |
| **No LLM in the path** | The live path is parser -> engine -> pricing -> broker. An LLM turn is unbounded and nondeterministic, and 9/8 is the proof that a queue between a post and an order is the failure. The LLM stays in the gapper desk, ops, and digest; it never sits between a post and an exit. | Deterministic code, `fuzz` determinism, replay parity, and the `NO_TRADE` kill switch. |

### Known unported surfaces (from 3.a) — not silent omissions

| PR#1 surface | Status in PR#2 | Why it stays out for now |
|---|---|---|
| `guard.evaluate` / `DEFAULT_CHECKS` (drift, vol, tail, burst, cooldown) | **Not ported** | The live lane does not have the research inputs those checks need. Porting them as empty stubs would be worse than not having them; the fail-closed *philosophy* is ported as `spread_ok`. |
| `reconciliation.reconcile` / `reconcile_and_halt` (`phantom_local`, `missing_local`, `quantity_mismatch`) | **Not ported** | Needs a venue positions/orders snapshot the live adapter does not yet have. This is **O2** below. O1's log/ledger seam is closed in final integration, but without a venue snapshot there is still no *automatic* drift halt: a human or scheduled `verify`/`recover` must act. |
| PR#1 `outbox._dispatch_one` UNKNOWN quarantine for a sender exception | **Ported in final integration** | `_on_transport_failure` no longer writes a terminal `reject`. It writes a `transport_unknown` quarantine row, leaves the intent unresolved so `verify`/`recover` can settle it from a lookup, and pages immediately when the unknown order is an **exit** (the engine books flat at intent time, so that is the asymmetric risk). The idempotency key remains the duplicate backstop; the quarantine is the visibility backstop. |

---

## 3.a red-team record (verbatim)

Task 3.a was read-only: five PR#1 modules, their live ports, and the delivered 2.a/2.c
code. No project files were modified. The gate evidence and the remaining numbered
findings are reproduced here; numbered findings 1-9 appear verbatim under the gem that
enforces them, and 10-14 are below.

### Gate / suite / corpus evidence

| Command | Result |
|---|---|
| `./scripts/gate.sh` (live) | ruff clean · mypy 27 files · **513 passed** · "all three clean" |
| `.venv/Scripts/python.exe -m pytest` (live) | **513 passed in 5.86s** |
| `python -m scorpion_mff.cli backtest` | alerts 93/59 · taken 91 · skipped 2 · contracts 405 · **P&L $57,951.00** |
| `python -m scorpion_mff.cli replay tapes/sep8.json` | events 4 · buys 2 (6 contracts) · **realised $211.00** |
| `python -m scorpion_mff.cli expiry audit` | 40/93 same-day · 65/93 no close (floor 61) · 0/40 cutoff force-close · 10/40 no exit time · assignment **REFUSED** |
| `python -m scorpion_mff.cli fuzz --iterations 5000 --seed 7` | **CLEAN**, all 10 invariants held |
| `.worktrees/pr1 $ ./scripts/gate.sh` | ruff clean · mypy 63 files · **551 passed, 1 xfailed** · "all three clean" |

These are the 3.a-time counts. Final integration added three transport-quarantine tests in
`tests/test_ledger.py`, so the live suite is now **516 passed**; every pinned corpus number
is unchanged. The verbatim findings below are left exactly as 3.a wrote them.

### Findings 10-14, verbatim

> 10. **PASS — cross-lane imports: none.** No `import scorpion`, `from scorpion`, `worktrees`, or `sys.path` injection into `scorpion_mff/`. The only `sys.path` line is `tests/conftest.py:9-10`, which adds the repo root (not the pr1 tree). `execution.py` imports `from .types import Quote` (local) and explicitly does not import the pr1 tree (`execution.py:1-6`).

> 11. **PASS — third-party imports: only optional lazy `websockets`.** AST scan of all `scorpion_mff/*.py` for non-stdlib top-level imports returned exactly one: `scorpion_mff/listener.py → websockets`, imported lazily inside `WebsocketGateway.messages` (`listener.py:291`) and declared optional in `pyproject.toml` (`gateway = ["websockets>=13"]`, `dependencies = []`).

> 12. **PASS — 40/93 same-day, no fabricated underlying/bars.** `historic.load_trades` gives 93; same-day (expiry==date or `SAME_DAY_TOKENS`) = **40/93**. Raw `trades.json` keys are exactly `{channel, date, entry_bid, exit_pct, expiry, notes, outcome, side, strike, ticker, time_et}` — **no** underlying/spot/bar/OHLC field exists. Backtest consumes only `entry_bid` and `exit_pct` (`historic.py:225-282`); the ask is a disclosed model (`ask_multiple`, `historic.py:9-19`), not a hidden invention.

> 13. **PASS — PR#1 gate green, read-only.** `git -C .worktrees/pr1 status --short` is empty (branch `hardening/deterministic-control-plane`, clean), and `./scripts/gate.sh` inside the worktree passed all three: ruff clean, mypy 63 files, pytest 551 passed + 1 xfailed.

> 14. **PASS — reported test counts.** `test_execution.py` 13 passed (2.c claimed 13), `test_expiry_engine.py` 38 passed (2.a claimed 38), `test_ledger.py` 18, `test_expiry.py` 71, `test_calendar.py` 48.

### Red-team observations (risks; not "failed claims") — verbatim

> - **O1 — Transport-failed SELL is terminal `reject`, not PR#1's UNKNOWN.** `_on_transport_failure` (`listener.py:181-197`) writes `reject` (reason `broker:transport:…`). PR#1 `outbox._dispatch_one` quarantines a sender exception as UNKNOWN for reconciliation (`outbox.py:181-188`). Because the hot path removes the position at *intent* time (`engine._on_close`/`_expiry_flatten` do `del self.positions[key]` before the intent is returned) and `reject` is in `Ledger.TERMINALS` (`ledger.py:23`), a transport error on an exit leaves the local book flat while the venue may still hold risk — and `recover` will never revisit it (no unresolved intent). The idempotency key in the HTTP header (`broker.py:211-216`) is the only backstop.

> - **O2 — No reconciliation halt in the live lane.** `phantom_local`/`missing_local`/`quantity_mismatch` drift detection (`reconciliation.py:88-135`) has no live equivalent, so O1's "unhedged remote" case has no detector. A natural 3.b/4.x follow-up.

> - **O3 — Stale `__init__.py` layering note.** `scorpion_mff/__init__.py` docstring lists `calendar`/`expiry` but omits the new `execution.py` and `ledger.py` from the layering map (cosmetic).

> - **O4 — Backtest clock caveat (already disclosed).** `historic.py` uses a fixed `ET = UTC-04:00` (`historic.py:40,47`) whereas the live guard uses the DST-aware calendar. No corpus trade (2026-03-10..09-03, all within US DST) is affected, and both `BACKTEST.md` and the audit's `clock_caveat` say so — so it is disclosed, not hidden.

### Unresolved items (explicit)

- **O1 — resolved in final integration (log/ledger seam).** The live lane no longer writes
  a terminal `reject` on a transport failure. `_on_transport_failure` writes a
  `transport_unknown` quarantine row, so the intent stays unresolved, `verify` names it
  (`[transport unknown: ...]`), and `recover` settles it from `SEEN`/`NOT_SEEN` without
  ever placing or retrying. A transport-failed **exit** also pages immediately
  (`exit_unknown:transport`). What is still true: the local book is flat at intent time, so
  until a lookup settles the row, the venue state is genuinely unknown. Do not "fix" that
  by making `recover` retry.
- **O2 is unresolved.** No **automatic** book-vs-venue drift halt exists. O1's ambiguity is
  now visible and resolvable, but there is no venue positions snapshot to detect drift on
  a schedule. It is recorded as an unported surface, not claimed as delivered.
- **O3 — resolved in final integration.** `__init__.py` now lists `execution` and `ledger`
  in the layering map.
- **O4 is resolved by disclosure.** No corpus trade is affected; the caveat is printed by
  `expiry audit` and repeated in `BACKTEST.md`.

### Integration resolution (final turn) — what changed and how to prove it

| Gap | Change | Acceptance |
|---|---|---|
| O1: transport-failed order closed as `reject` | [listener.py](../scorpion_mff/listener.py) `_on_transport_failure` now writes `transport_unknown` (non-terminal), pages on an unknown exit, and keeps the breaker/one-page rule. [ledger.py](../scorpion_mff/ledger.py) gains `quarantine_reasons` so `verify` can name the cause. | `python -m pytest tests/test_ledger.py -q` -> 21 passed (3 new transport-quarantine tests) |
| O3: stale layering map | `__init__.py` docstring lists `execution` and `ledger`. | `python -m pytest tests/test_ledger.py -q -k recover` -> 3 passed |

No new order path was added: the quarantine is bookkeeping only, and `recover` still cannot
place. The corpus backtest and the 9/8 replay are unchanged (they have no transport errors).

### Handoff from 3.a, verbatim

> Everything claimed in reports 2.a and 2.c that is checkable from this repo **holds**: ET roll, fail-closed calendar, never-silent slot release, three-valued expiry verdict, cutoff force-close at a live bid / page on silence, tick-without-traffic firing, breaker threshold/cooldown/one-probe, buys-gated/exits-ungated, spread-gate-off-by-default replay parity, and the 93/59 · 91/405 · $57,951.00 · $211.00 pins.

> No failed claims found. Two *unported* PR#1 surfaces (guard check set, reconciliation drift-halt) were never claimed as ported, and two *risks* worth the next writer's attention (O1 transport-exit ambiguity, O2 no venue-drift detector) are called out above with file:line.

> Read-only confirmed: `git status` shows no changes beyond what prior writers left; the only writes I made were pytest/ruff/mypy caches and temp-dir fixtures (in `%TEMP%`, not the repo).

---

## Reviewer sequence

The short version, in the order that fails fastest:

```bash
./scripts/gate.sh                                            # ruff -> mypy -> pytest, 736
python -m scorpion_mff.cli backtest                          # 93/91/405/$57,951.00
python -m scorpion_mff.cli replay tapes/sep8.json            # 4/2/6/$211.00, 0 breaches
python -m scorpion_mff.cli expiry audit                      # 40/93, 65/93, 0/40, 10/40, REFUSED
python -m scorpion_mff.cli fuzz --iterations 5000 --seed 7   # CLEAN
python -m scorpion_mff.cli doctor                            # spread gate OFF, broker DRY
```

Then the gem suites (each is one command; see the index above):

```bash
python -m pytest tests/test_calendar.py -q                                  # 48
python -m pytest tests/test_expiry_engine.py -q                             # 38
python -m pytest tests/test_expiry_engine.py -q -k "cutoff or tick"         # 12
python -m pytest tests/test_execution.py -q -k breaker                      # 6
python -m pytest tests/test_execution.py -q -k spread                       # 5
python -m pytest tests/test_ledger.py -q                                    # 21
python -m pytest tests/test_ledger.py -q -k recover                         # 3
python -m pytest tests/test_gateway.py -q                                   # 20
python -m pytest tests/test_risk_budget.py -q                               # 24
python -m pytest tests/test_execution_book.py -q                            # 66
python -m pytest tests/test_venue_feed.py -q                               # 47
python -m pytest tests/test_confirmed_exits.py -q                          # 14
python -m pytest tests/test_flatten.py -q                                  # 14
```

A failure in any of these is a gem failing, not a flaky test: every command is offline,
seeded where stochastic, and independent of credentials. Nothing here places an order.

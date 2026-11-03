# The executor, run against every alert on record

`mff-backtest/bt_full/trades.json` — **93 alerts across 59 trading days**, 2026-03-10 to
2026-09-03, all four channels. This runs the *same* parser, state machine, pricing and
sizing that would run live; nothing is stubbed except the market.

    python -m scorpion_mff.cli backtest --attribution --md reports/executor-backtest.md

The machine-readable form of the same run is `--json`. The generated table report is
`reports/executor-backtest.md`.

## What this is, and what it is not

**It measures the executor, not the strategy.** The channel's calls are the input. What is
being tested is whether the locked rules can act on them: how many contracts, at what
limit, taken or refused and why.

**The corpus is his own record of calls, and it is winners-heavy** — 57 win_partial, 24 win,
6 loss, 2 scratch, 4 unknown. So the P&L below is a *calibration* number ("what would the
hone have done with the trades he says he made"), not a forecast of edge. A corpus of calls
a trader chose to post cannot price the calls he did not post.

**Three assumptions are load-bearing** and are exposed as flags rather than buried:

| assumption | default | flag |
|---|---|---|
| ask = his posted bid × N (the corpus never records the ask) | 1.05 | `--ask-multiple` |
| exit at his posted level × N | 1.0 | `--exit-slippage` |
| exits are emitted naming the ticker, as the channel does | on | `--bare-exits` |
| ET modelled as a fixed UTC-04:00 offset | — | — |

## Headline (as locked: ask ×1.05, pipe test on)

| metric | value |
|---|---|
| alerts | 93 over 59 days |
| taken | 91 |
| refused by the locked rules | 2 (both NKE) |
| contracts | 405 |
| notional | $67,400 |
| realised P&L | $57,951 |
| average per taken trade | $637 |
| win rate | 90.9% (80W / 8L) |

By channel: options-king $23,512 (41 alerts) · high-confidence-options $21,143 (9) ·
etf-options $9,166 (34) · platinum-options $4,130 (7).

The 80/8 split reconciles with the desk's own record (81 win-class alerts): the difference is
the two NKE alerts the lock excludes and the three alerts that record no exit.

## What each locked rule is worth, one rule changed at a time

| variant | taken | contracts | P&L | Δ vs locked |
|---|---|---|---|---|
| as locked (ask ×1.05, pipe test on) | 91 | 405 | $57,951 | — |
| no pipe test — full clip from the first trade | 91 | 839 | $133,537 | **+$75,586** |
| fill at his bid (ask ×1.00) | 91 | 421 | $62,403 | +$4,452 |
| 30% through his bid — the +25% gate fires | 1 | 1 | $44 | **−$57,907** |
| exits at 90% of his posted level | 91 | 405 | $45,462 | −$12,489 |

Read these carefully:

* **The +25% gate is not a nicety — it is the rule that decides whether the hone trades at
  all.** At a 30% ask, 90 of 91 alerts are refused. That is the difference between a bot that
  works and one that sits dead while the channel makes money, which is exactly the 9/8
  failure from the other direction.
* **The pipe test costs $75.6k of upside on this corpus** — and buys something the corpus
  cannot price: proof that a fill is real before sizing up. On a winners-only record,
  insurance always looks expensive. That is why it is not tuned away; it is the rule that
  would have caught a dead broker on the one morning it mattered.
* **The 5% ask multiple costs $4.5k.** That is the price of not chasing a print.
* **A 10% exit haircut costs $12.5k** — the model's single biggest sensitivity, and the
  reason exits are the place to spend engineering effort next (see "what this does not
  cover").

## The two defects this found in the hot path

Driving 59 days of real alerts exposed failure modes a single-day tape cannot:

**1. A position never released its concurrency slot.** The first run took 8 of 93 alerts and
skipped 83 with `max_concurrent:2` — because an alert with no recorded exit (the corpus has
three, plus one whose exit post could not be attributed) held a slot forever. On the live
path that is a hone that goes silent mid-month and never says why: the max-2 limit turns one
missed exit into a permanent shutdown. Fixed by releasing positions whose contract has
provably expired, at the day roll (`Engine._expire_carried`):

  * `0DTE` in the key → released at the next day's roll (an ET trading date, from
    `calendar.py`, not the UTC date: at 01:30 UTC a 2026-09-11 contract is still the 11th
    in New York);
  * an ISO expiry earlier than the ET trading date → released;
  * anything unparseable (`Dec 5`) → **not** released, and an `expiry_unparsed` alert is
    raised, because freeing a live slot on a guess under-counts risk;
  * the release is recorded with `accepted=False`, so an unmarked expiry never enters
    realised P&L - and it also writes an `expiry_unresolved` alert carrying the projected
    assignment shares, or `unknown` when there is no underlying close to project from.

`expiry audit` measures the same corpus for the cutoff guard: **40 of 93 alerts are
same-day**, **65 of 93 never record an explicit final close** (floor 61 = 57 `win_partial`
+ 4 `unknown`), **0 of 40 same-day exits land after the 15:45 ET cutoff** and 10 carry no
exit time at all (at the cutoff those page rather than flatten). Assignment is explicitly
**not** projected: the corpus has no underlying close, and a missing price is not OTM.

**2. An exit could not be attributed unless it was the only position in its channel.** The
parser only recovered a ticker from the literal `TICKER X` form, so `QQQ puts closed at 2.85`
arrived with no ticker and the engine could only resolve it by channel — with two positions
open in that channel, every exit became `alert:ambiguous_exit` and the book could not empty.
Fixed by recovering a *bare* symbol on exit-side posts, under a strict rule: exactly one
candidate token that is not a known English word, otherwise nothing. The engine still refuses
to choose between two symbols; ambiguity remains an alert, never a guess.

Both have regression tests (`tests/test_exit_attribution.py`), including the case that
matters most: two positions, one named exit, the other left untouched.

## What this does not cover

* **No slippage on the entry fill itself.** The engine prices a limit; whether the option
  actually filled at that limit is a market question this corpus cannot answer. That is what
  the pipe test is for, and what the live quote feed has to prove.
* **No partial fills, no rejects, no halts.** The corpus has no fill reports, so every
  accepted limit is assumed filled.
* **No theta inside the hold.** Exits happen minutes after entries, so decay during the
  hold is negligible — but a position that is not exited before expiry was marked at entry,
  which hides the real loss. **That gap is now measured** (`TCA.md`): of the 18 corpus trades
  with contract bars, **17% finished at ~zero**, and the median contract was already
  **−12.9% below our entry** by the 09:30 open on the day it traded. Marking an unexited
  expire at entry is not a rounding convenience — on the trades we can see, missing the exit
  gives back the premium.
* **The exit levels themselves remain unverified.** The bars cannot settle whether his posted
  exit was attainable, because 16 of the 18 matched trades entered pre-market while the series
  begin at 09:30 — the move happened before the data starts. A path statistic averaged over a
  window that does not contain the trade would be a fiction, so `tca path` withholds its
  aggregates below five measurable trades rather than printing one.
* **Nothing here says the channel's edge continues.** It says the executor would have acted
  on the record as written, at the sizes the lock allows, and it says where the rules that
  are load-bearing actually bite.

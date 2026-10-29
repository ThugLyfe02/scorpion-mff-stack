# The next 20,000 lines: what earns them

A plan written to be argued with. Every line below has an **acceptance test** - a command whose
output either satisfies the claim or doesn't. Anything that cannot name one does not belong here,
because a plan without acceptance tests is a wish list with line counts attached.

Ground rules that shape the ordering:

* **Nothing is built twice.** No module may re-derive a key, a price or a policy that another
  module already owns. The replay defect in this branch was caused by one state transition living
  outside the log, and duplication is the same failure in a different costume.
* **Money-path code ships with its own failure test.** Any module that can send, size or settle
  must have a test that proves the failure mode is closed, not that the happy path works.
* **Data before analysis.** Three separate analyses are already withholding their answers
  (mark-out 3/107, TCA 2/18, attribution on 91/93) because the inputs are missing. Building the
  fourth analysis before the data plane is how a research desk becomes elaborate fiction.
* **Blocked work is sequenced last, not dropped.** Credential-gated and bar-gated work stays in
  the plan with its blocker named, so the order can change the day the blocker clears.

---

## Phase 1 - Data plane (3,200 lines) · unblocks everything else

**1.1 `bars.py` - bar ingestion, normalisation, integrity (1,400)**
Reads the vendor bar formats already present (`week_bars.json` shape, `charts_1m`), validates
strictly, and stores into the same SQLite database as the applied log so a mark-out query is one
plan, not a join across files. Rejects: non-monotonic clocks, duplicate timestamps, bars whose
high/low bracket fails, sessions shorter than N bars, and any set whose timezone cannot be proved.
*Acceptance:* `scorpion bars ingest --source <path>` reports accepted/rejected counts with a named
reason per rejection; `scorpion bars coverage` prints, per contract, session length and gaps.
*Why it earns 1,400:* three analyses are currently blocked on this exact join, and every future
one will be too.

**1.2 `coverage.py` - the measurement ledger (900)**
Which contracts have bars, for which sessions, at what resolution, and what cannot be measured as
a result. Produces the number that has been missing from every report so far: **what fraction of
the corpus is measurable**, per channel and per month.
*Acceptance:* `scorpion coverage --corpus ... --bars ...` prints measurable/total per channel and
lists each unmeasurable trade with its reason and the exact missing input.

**1.3 `sessions.py` - market calendar, half-days, holidays (900)**
Expiry semantics depend on it (`0DTE` is not "same calendar day" on a half-day), and the day-roll
that caused the replay defect depends on it too.
*Acceptance:* property test that every corpus day maps to a session with open/close and that no
two adjacent sessions overlap; half-days pinned explicitly.

---

## Phase 2 - Measurement at scale (3,400) · needs Phase 1

**2.1 `markout.py` extension to full-corpus study (1,200)**
Per-trade entry drift, MFE/MAE, capture, left-on-table; aggregates per channel, per hour-of-day,
per contract type; explicit withholding where coverage is thin.
*Acceptance:* `scorpion markout --full` produces a capture distribution over ≥80% of taken trades,
or refuses with the coverage number that explains why not.

**2.2 `attribution.py` - counterfactual engine (1,300)**
Runs the deterministic core under policy variants (no pipe test, wider gate, different markup,
exit at 90% of claim) and attributes P&L delta per rule with the same common-random-number
discipline already used in `ruin.py`, so a delta is a threshold rather than noise.
*Acceptance:* `scorpion attribute --variants <file>` yields a table where each rule's contribution
reproduces the total delta within 0.01R.

**2.3 `survival.py` - holding-time and hazard (900)**
Time-to-exit distribution, hazard by channel, and the share of trades that would have been better
off held to the close. This is the analysis that decides whether exits belong to a human or a rule.
*Acceptance:* Kaplan-Meier-style curve with censoring reported honestly (open positions at
session end are censored, not dropped).

---

## Phase 3 - Execution realism (3,600) · needs Phase 1 for fills

**3.1 `lifecycle.py` - scale-outs, adds, runners (1,600)**
The corpus is full of "sold half at 25%, runners to 75%". The book models one exit; reality is a
sequence. This module turns a claimed lifecycle into an ordered set of intents, with the invariant
that partial exits can never exceed held quantity and a runner can never be sold twice.
*Acceptance:* property test over 10k generated lifecycles: sum of scale-outs ≤ entry quantity,
monotone timestamps, no double-sell; plus a corpus replay that reproduces claimed R within 1%.

**3.2 `fills.py` - partial fills, rejects, requotes (1,100)**
An order that fills 3 of 5 changes every downstream number. Models partial fills as first-class,
with the rule that a partial never counts as a completed intent.
*Acceptance:* scenario where a 5-lot fills 3 then 2 reproduces the same realised R as one 5-lot
fill, and a fill of 2-only leaves the position open with the remainder tracked.

**3.3 `venue.py` - the real Sender (900)** · **blocked: one real ack payload**
The protocol seam is already shaped for it. This wires `PaperSender → HttpSender` with no other
change, proving the seam was right.
*Acceptance:* a recorded ack replayed through `HttpSender` produces a `Receipt` byte-identical to
the paper path's, and a token never appears in any error string.

---

## Phase 4 - Risk at portfolio level (2,800)

**4.1 `exposure.py` - correlated book, concentration, expiry clustering (1,400)**
Two QQQ calls and a SPY put are not three independent bets. Computes gross/net delta-equivalent
exposure using strike distance as a proxy (documented as a proxy, not a greek engine), flags
concentration by underlying and by expiry, and caps by exposure rather than by position count.
*Acceptance:* a book of 2 correlated index calls reports higher effective exposure than 2
uncorrelated names, and the cap refuses the third correlated entry.

**4.2 `ruin.py` extension - path-aware sizing (800)**
Sizing per trade ignores that a sequence of losses within one day is one event. Adds intraday
clustering to the bootstrap and reports the cost of the pipe test at portfolio level.
*Acceptance:* reported `p_down` rises when losses are clustered vs independent, and the difference
is quantified.

**4.3 `governor` integration (600)**
The risk layer exists in `scorpion_mff`; this port brings stand-down semantics into the control
plane as a first-class applied event (`kind="halt"`), so a halt is in the log and a replay
reproduces it. This closes the same class of hole the sweep had.
*Acceptance:* a halted store replays halted, and `verify` is clean with a halt in the log.

---

## Phase 5 - Operations (2,600)

**5.1 `supervisor.py` - liveness, heartbeat, restart policy (900)**
Heartbeat writes, staleness detection, and a restart that resumes from the snapshot without ever
resuming halted-but-forgotten. Exit codes designed for cron.
*Acceptance:* killing the process mid-send leaves an UNKNOWN row that recovery quarantines; a
restart on a halted store sends nothing.

**5.2 `metrics.py` - counters, timers, and an export that isn't a dependency (800)**
In-process registry + a text/JSON export, no Prometheus client. Counts what matters: effects
proposed/sent/refused/unknown by reason, dispatch latency, replay divergence count.
*Acceptance:* `scorpion metrics` prints a stable schema; divergence count is 0 in a clean run and
>0 when a row is deleted.

**5.3 `audit.py` - immutable export and drill harness (900)**
Exports a day's decisions, fills, refusals and verification result as one signed-in-content
bundle (digest, not crypto), plus a drill that replays a random day and asserts equality.
*Acceptance:* `scorpion audit export --day <d>` produces a bundle whose digest reproduces from
the log alone; `scorpion audit drill --days 5` passes on a healthy store.

---

## Phase 6 - Strategy research (2,400)

**6.1 `edge.py` - channel decay and stability (1,200)**
Per-channel R over time with confidence bands, detecting a channel whose edge is decaying before
it costs money. Explicitly refuses to fit a trend to <20 observations.
*Acceptance:* a channel with a synthetic decay is flagged; a channel with 12 trades reports
"insufficient" rather than a slope.

**6.2 `selection.py` - EV gating and strike choice (1,200)**
Uses measured distributions to answer: at what premium is this trade positive-EV, and is the
strike he chose the best available? Turns the sizing table from research-derived to data-derived.
*Acceptance:* EV per trade computed from the corpus with the loss-rate stress applied; the
recommended premium band changes when the stress level changes.

---

## Phase 7 - Hardening (2,000)

**7.1 `faults.py` - an injectable failure matrix (1,200)**
Every layer gets an injectable fault: parse garbage, quote from the future, sender timeout at each
stage, disk full on append, snapshot digest mismatch, clock skew. One harness, one report.
*Acceptance:* `scorpion faults --matrix` runs all faults and asserts each is contained (no
unhandled exception, no duplicate order, no silent state change).

**7.2 Soak and property suites (800)**
10k-message soak with a fixed seed, asserting `verify` clean at the end and no unbounded growth in
the applied log or snapshot size.

---

## What is deliberately NOT in these 20,000 lines

* No web UI, no dashboards, no notebook layer - the CLI and the JSON reports are the product.
* No ML. Every claim here is arithmetic on a corpus; a model would hide the arithmetic.
* No live-money features behind credentials. `venue.py` is a seam, and its acceptance test runs
  on a recorded payload.
* No abstraction layer "for future venues". One venue, one adapter; the second one will teach us
  what the abstraction should have been.

---

## Budget

| Phase | Lines | Blocked by |
|---|---|---|
| 1 Data plane | 3,200 | nothing |
| 2 Measurement | 3,400 | Phase 1 |
| 3 Execution realism | 3,600 | bars for 3.1/3.2, ack payload for 3.3 |
| 4 Portfolio risk | 2,800 | nothing |
| 5 Operations | 2,600 | nothing |
| 6 Strategy research | 2,400 | Phase 1 + 2 |
| 7 Hardening | 2,000 | everything above |
| **Total** | **20,000** | |

Unblocked today: Phase 1 (3,200), Phase 4 (2,800), Phase 5 (2,600), Phase 7 (2,000) - **10,600
lines that need nothing from anyone.** Phases 2 and 6 need bars; Phase 3.3 needs one payload.

The order is not preference: it is dependency. Data plane first because three analyses are already
withholding answers for want of it, and a fourth would be built on sand.

# Master prompt: continuing PR #1

A working brief for any agent picking up `hardening/deterministic-control-plane`. Paste this whole
file as context. It exists because a branch this size fails through lost context, not through
missing skill: someone re-derives a decision that was already made, contradicts an invariant, or
re-fixes a bug that was fixed three commits ago.

---

## 1. Role and standard

You are the engineer of record for a trading control plane that will one day move real money. Not a
prototype, not a demo. The standard is the one a desk applies to its own execution code: every claim
backed by a command's output, every invariant enforced by a test, every refusal named.

Three rules that override enthusiasm:

1. **Never fabricate a result.** If a tool fails, say so and try another path. A wrong number in
   this domain is worse than a missing one, because it gets acted on.
2. **Never claim a milestone you have not run.** "verified: true" means you executed the corpus
   replay and saw zero divergences, not that the code looks right.
3. **Report the defect you found, especially when it is yours.** Every significant step in this
   branch surfaced a real bug. That is the expected rate, not a failure.

Tone: direct, specific, no filler. State numbers. State what is blocked and on what.

---

## 2. What this system is

An event-sourced control plane that turns a Discord alert into a sized, authorized, dispatched,
settled and audited option order - deterministically, and with a book that can be rebuilt from its
own log.

    message -> parser -> association -> reducer (risk) -> effects
            -> authorization (ladder of refusals) -> outbox (durable, no blind retries)
            -> venue (Sender) -> settlement -> applied log -> verification

The applied log is the truth. `verify` replays it and diffs against the live book, field by field.
If they disagree the desk halts, because a book that cannot explain itself must not trade.

---

## 3. Invariants. Break one and the branch is wrong.

**Event sourcing**
- Every state change is an applied event: `signal`, `fill`, `expire`, `halt`. Nothing mutates the
  book outside the log.
- If you add a code path that closes, opens or halts a position, it MUST append an applied row and
  `apply_applied` MUST reproduce it. The expiry sweep and the halt flag both violated this and both
  produced real defects.
- `replay_applied(rows) == live_state` at every prefix, not just at the end.

**Orders**
- No order leaves the process without: `--live`, a configured quote feed, a valid non-stale policy,
  and an authorization envelope.
- A transport exception is AMBIGUOUS. Quarantine as `UNKNOWN`, never retry. Only an explicit
  retryable refusal from the venue is safe to retry.
- Idempotency keys are recorded at send time and read back. Never re-derived; a second copy of the
  format drifts silently.

**Risk**
- Reduce-only is structural: enforced at load, at decision (minimum composition), and again at
  application.
- A halt is terminal until a human clears it, and the clearing is itself an event.
- A release frees the quantity as well as the slot. A released position holding exposure is what
  jams the day roll.

**Data**
- No timezone is assumed. The corpus's `time_et` column is **Pacific**, proved by measurement
  (a +3h shift put joinable trades at 13:31Z and 13:37Z, one and seven minutes after the open).
- A bar set spans exactly one session. Two shipped sets do not, and are refused.
- An unmeasurable trade is named with a reason, never silently dropped.

**Research**
- Never average below the floor. Withholding an aggregate is a deliverable.
- Bootstrap and simulation use common random numbers across compared scenarios, so a difference is
  a threshold rather than two unrelated draws.
- State the assumptions that make a number optimistic, in the same breath as the number.

---

## 4. The gates. All three, every commit.

    ruff check src tests            # E,F,I,B,UP,SIM
    mypy src/scorpion               # --strict, no ignores without a reason
    pytest                          # corpus-scale tests included; ~15s

Never push with a gate red. If one goes red, fix it before the commit, not after - a red push
invalidates everything downstream that trusts the branch.

Every commit message states: what the defect was, why it was invisible, what proves the fix, and the
gate output. Corpus numbers are pinned in tests; if a rule or the parser drifts, a test fails rather
than a number quietly moving.

---

## 5. Where the current state actually is

    src/scorpion/          36 modules, ~7,950 lines
    tests/                 2,337 lines, 206 tests
    applied log            107 signals, 173 fills, 3 releases, 3 halt events
    corpus replay          88/107 taken, 327 contracts, +87.59R vs +90.58R claimed
    verification           ok: true, zero divergences
    coverage               3/107 measurable (2.8%) - 97 contract-days have no bars

Proven: deterministic executor, replay guarantee, compaction, sending discipline, circuit breaker,
corpus ledger, risk-of-ruin (78.1% breakeven win haircut), mark-out (withholding), mark-to-market
(withholding), calendar and coverage, halt-as-event, audit bundles, supervision and metrics,
executable lifecycles.

Not proven: live venue `Sender` (needs one real ack payload, tokens stripped), live quote feed,
mark-out at scale (needs bars for 97 contract-days), partial fills, portfolio-level exposure.

---

## 6. What to build next, in order

1. **`attribution.py`** - policy variants through the real pipeline, delta attributed per rule,
   common random numbers. Now that lifecycles exist, "no pipe test" and "exit at 90% of claim" are
   measurable rather than asserted. Acceptance: each rule's contribution reproduces the total delta
   within 0.01R.
2. **`fills.py` partial fills** - a 5-lot filling 3-then-2 must produce the same realised R as one
   fill; a 2-only fill leaves the remainder tracked.
3. **`exposure.py`** - correlated book: two index calls plus a put are not three independent bets.
   Acceptance: the cap refuses a third correlated entry.
4. **`audit drill`** - replay a random day, assert equality, emit a pass/fail an operator can read.
5. **`faults.py`** - injectable failure matrix: garbage parse, quote from the future, sender timeout
   at each stage, disk full, snapshot digest mismatch. Each fault must be CONTAINED, not survived.
6. **Bar ingestion at scale** - `scorpion coverage --db` already persists into the applied-log
   database, so this is a fetch loop. It moves measurability from 2.8% to ~90% and unblocks every
   measurement milestone.

Do them in that order. The order is dependency, not preference.

---

## 7. Anti-patterns that have already cost this branch real time

- **Module-level clock constants.** Effects are stamped with the wall clock, so a dispatch clock
  from a constant is never "due" and the test asserts against an empty queue. Take clocks at call
  time. This produced a zero-trade corpus replay.
- **Querying a schema you did not read.** `executions` has `fill_qty`/`fill_price`, no `generation`.
  A report nobody runs is a report nobody has tested.
- **Trusting a summariser.** A subagent's "397 passed" was a claim; the run had to be re-executed.
- **Assuming a timezone, a column name, or a session length.** All three have been wrong here.
- **A verifier that raises on the first bad row.** It should list every divergence; the library
  path can still fail loudly.
- **Committing small.** Each commit should be one coherent capability with its tests and its gate
  output, not a fragment. If a change spans two modules and a test file, that is one commit.
- **Surgically editing inside a long docstring.** Replacing one line of a triple-quoted block with a
  shorter one silently deletes the closing quotes and swallows the rest of the file; the syntax error
  surfaces hundreds of lines later, at the last `"""`. This happened twice in one session. When a
  docstring needs fixing, replace the WHOLE block between the opening quotes and the first import.
- **Writing lines longer than 100 characters.** Ruff's E501 then demands a follow-up edit to a line
  that was already correct, and that edit is where the docstring damage happens. Stay under 100 while
  writing, not after.
- **Running pytest before ruff.** A green test run on a file that fails E501 is not green; the lint
  fix afterwards is the one that breaks things. Lint first, then test.
- **Batching three modules without a mid-point gate.** A 1,100-line commit is three ~350-line modules
  each linted and tested before the next is started, then committed together. Writing all three and
  verifying once means the failure report names a 1,100-line surface instead of a file.

---

## 8. How to verify you are done with a piece of work

1. A command that demonstrates the new capability, run for real, with its output quoted.
2. A test that fails if the invariant is removed - not a test that merely executes the code.
3. The three gates green, in one run, after the last edit.
4. The commit message naming the defect class, not just the feature.
5. The docs update: `docs/ROADMAP_20K.md` phase marked, or `docs/REPLAY_GAP.md` if a defect.

If any of the five is missing, the work is not done - it is merely written.

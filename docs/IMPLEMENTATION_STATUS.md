# V2 hardening implementation status

Implemented in this change:

- [x] deterministic domain model
- [x] immutable UTC Discord event schema
- [x] push-based Discord ingest component
- [x] allowed guild/channel filter by ID
- [x] deterministic parser with explicit ambiguous path
- [x] conservative follow-up association
- [x] pure event reducer
- [x] event idempotency
- [x] generation/stale-event controls
- [x] max-open-position control
- [x] one-add-per-generation control
- [x] halt state
- [x] SQLite WAL durable event/audit schema
- [x] crash recovery by replay + effect re-materialization
- [x] Discord author allowlist required in runtime mode
- [x] Discord reply/reference contract association
- [x] heartbeat storage
- [x] paper broker
- [x] review-only execution boundary
- [x] entry limit/dislocation helpers
- [x] deterministic replay
- [x] conservative entry-bar OHLC handling
- [x] corrected partial-exit cost-basis primitives
- [x] corrected concurrency primitive
- [x] package metadata
- [x] lint/type/test CI
- [x] security/history audit documentation
- [x] unit tests for parser, reducer, store, replay, pricing
- [x] deterministic clip sizing (`sizing.py`): whole contracts from a dollar budget, floored,
      never rounded up, never zero for a decided trade, capped per clip
- [x] realised P&L per fill (`settlement.py`): proceeds minus cost basis on every reduction,
      explicit for adds (zero) and finals (whole remainder), refusing impossible fills
- [x] risk limits as book state (`risk.py`): 1R in dollars, daily stop, drawdown ladder,
      drawdown-from-peak, loss streaks, channel tilts - **reduce-only**, enforced at config
      load, at decision time (minimum of every binding rule) and again at apply
- [x] expiry sweep (`expiry.py`): a contract that expired stops holding a slot; unparseable
      expiries are reported and never released
- [x] executable authorization ladder (`authorization.py`): proposal -> envelope with a named
      refusal for each reason (stale/crossed/future quote, dislocation, generation, halt,
      zero size, live disabled) and a generation-specific idempotency key
- [x] risk state carried in `BookState`, so replay reproduces the drawdown and a restart
      cannot forget a stand-down
- [x] durable outbox dispatcher (`outbox.py` + `outbox_state`/`executions` tables): leased
      claims inside `BEGIN IMMEDIATE`, attempts counted at claim, exponential backoff with a
      ceiling, dead-letter after the budget, refusals terminal, idempotent execution records
- [x] reconciliation (`reconciliation.py`): both directions of drift - phantom local and
      unhedged remote - with a per-contract explicit tolerance; drift halts the book and
      emits a HALT effect rather than a log line
- [x] TCA on realised fills (`tca.py`): limit vs fill in bps and dollars, partial-fill share,
      latency p90, and a `suggested_multiple` (p90 of fill/reference, rounded up) that
      refuses to answer below ten priced fills
- [x] paper venue (`paper.py`): a real `Sender` implementation, deterministic, with scripted
      failure injection - so the whole loop runs without credentials or network
- [x] vertical slice (`scenario.py`) + operator CLI (`scorpion demo|dispatch|outbox|tca|
      reconcile|health`): one command drives parse -> risk -> authorize -> dispatch -> settle
      -> TCA and prints the trail; the same runner is the shadow harness when a real Sender
      is wired
- [x] a persisted halt is read back on start (`Store.halt_state`), so a restart resumes
      halted instead of quietly trading
- [x] sending-state discipline: a claimed effect becomes SENDING; a transport exception is
      AMBIGUOUS and quarantined as UNKNOWN (never blind-retried), recovered on restart and
      resolved only by reconciliation (`recover_sending`, `resolve_unknown`, `scorpion recover`)
- [x] circuit breaker: consecutive transport failures open the breaker for a cooldown, then
      one probe; a dead venue cannot turn the queue into dead letters
- [x] an applied-event log (`applied_events`): fills are events, not side effects, so the book
      is a pure function of its log - and `scorpion verify` replays it and diffs the result
      against the live state, halting on any divergence
- [x] the long-running dispatcher (`scorpion service`, `service.py`): one bounded pass per tick
      (rebuild the book from its log, recover in-flight sends, reconcile, dispatch, settle), a
      jittered cadence with exponential idle backoff, and the two refusals a loop must honour -
      a halted desk never dispatches, and an UNKNOWN send blocks new orders until the venue says
      what happened (`--once` gives cron a single-tick form with the same exit codes)
- [x] a metrics series (`scorpion series`): one bounded JSONL row per tick carrying dispatch,
      reconciliation, unknown and halt state, summarised to totals, rates and the live tail -
      so "did the dispatcher stop dispatching" is a file's answer, not a guess
- [x] automatic reconciliation on UNKNOWN rows: the loop resolves every UNKNOWN send against
      the venue lookup on each tick it exists (the tri-state lookup is the only retry
      authority; it never blind-retries), and clears the block the moment the last one resolves

Still requires environment/operator work:

- [ ] make GitHub repository private (repository-admin setting)
- [ ] purge sensitive values from existing Git history
- [ ] provision Discord bot/application token with minimum intents/permissions
- [ ] reconstruct raw historical Discord message-by-message corpus
- [ ] validate parser precision/recall on that raw corpus
- [ ] shadow run through live sessions before any operational reliance
- [ ] external reviewed brokerage integration, if desired, behind human authorization
- [ ] a concrete venue `Sender` behind `Dispatcher` (protocol is defined; no vendor is wired)
- [ ] position/cash snapshot feed for reconciliation (the comparison is built, the source is not)
- [ ] pre-market bars so exit levels and path statistics can be verified rather than assumed
- [x] compaction (`scorpion compact`): snapshot at a sequence with a sha256 digest, re-read
      it, and only then truncate - `keep_tail` preserves a forensic window; the resumed book is
      compared with the pre-compaction book in tests, and `verify_snapshot` re-checks the tail
- [x] UNKNOWN resolution (`reconcile_unknown`): a tri-state venue answer (seen / not_seen /
      cannot say); `NoLookup` leaves every row unknown rather than guessing "not sent", and the
      idempotency key is read back from the row recorded at send time, never re-derived
- [x] failure injection: every prefix of the applied log is replayed and compared with the
      state the live pipeline held at that point; a deleted row shows up as a sequence gap and
      a corrupted payload raises instead of being skipped
- [x] path signatures (`signature.py`): the truncated signature of a piecewise-linear path by
      exact tensor-exponential composition, the lead-lag transform of a univariate series, the
      Levy area (= signed polygon area, = half realized quadratic variation under lead-lag),
      and a fit gate that refuses a level-3 fit on the 107-trade corpus with a named reason
      (14 non-constant features, floor of 140 samples). See `docs/FRONTIER_7.md`.
- [x] out-of-sample policy tournament (`tournament.py`): chronological folds with purge and
      embargo, per-fold selection on the training block, an unseen-block score, Deflated Sharpe
      over the trial count, PBO by CSCV, and a paired block bootstrap. The shipped corpus selects
      the incumbent baseline in every fold, so the honest verdict is `INCUMBENT_HELD`, not a
      promotion. `scorpion tournament` returns 0 for `SUPPORTED`/`INCUMBENT_HELD` and 1 for
      `FRAGILE`/`REJECTED`/`REFUSED`. See `docs/TOURNAMENT.md`.
- [x] mark-to-market (`marks.py`): the operator view the book was missing - unrealised P&L per
      held position, at/through-cost flags, and a total that **withholds itself** (`None`) when
      any open position cannot be priced or can only be priced with a stale quote, so a partial
      sum never masquerades as the book's P&L. `split_stale` partitions quotes by age (a phantom
      bid must not price a position), and `scorpion marks --check` is the cron tripwire: exit 1
      when any position is at/below cost, unpriced, or stale-priced.
- [x] a monotone risk clock (`RiskState.rolled` + `Pipeline.effective_day`): a backdated event
      (a redelivered Discord message, a clock step, a replayed row) can no longer rewind the
      market day, reset `realized_today`/`consecutive_losses`, or re-arm a daily stop that
      already fired. The `faults` clock-skew case is now **contained** rather than an open
      `xfail`; the regression is pinned in `tests/test_risk_layer.py` and `tests/test_faults.py`.

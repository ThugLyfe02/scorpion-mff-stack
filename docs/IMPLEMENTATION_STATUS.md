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
- [ ] operator surface: a CLI that dispatches, reconciles, and prints TCA over the store

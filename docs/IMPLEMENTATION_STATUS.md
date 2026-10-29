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
- [x] calibration from adjudications: per-rule accuracy, Wilson lower bound, Brier score,
      calibration gap, and an `UNCALIBRATED / PROVISIONAL / TRUSTED / DEGRADED` state
- [x] durable semantic review queue (`review.py`, `review_items`): deterministic id per event,
      active-learning priority, evidence payload, claim/resolve/dismiss, SLA and reason metrics,
      and a `backfill` pass over stored unadjudicated signals
- [x] parser certification (`certification.py`): corpus loading, paired baseline/candidate
      comparison, accuracy/actionable precision/ambiguity gates, exact McNemar, ECE and Brier
      decomposition, risk-coverage selection, action-escalation refusal, downstream semantic
      state and effect-multiset diff, and a digested CERTIFIED/REFUSED certificate
- [x] parser pin (`parser_pin.py`, `scorpion-pin`): the handoff artifact a downstream lane
      can check without importing this one. `write` builds a pin only from a CERTIFIED
      certificate whose candidate IS the parser being pinned - a valid certificate for A
      pinned to B is refused - and records the sha256 of the module source that will run
      (line endings normalised, because a CRLF checkout is the same code), the corpus
      digest, the certificate digest and decision, and the criteria digest, plus the pin's
      own content digest. `verify` re-derives every digest and reports EVERY disagreement
      at once in a documented order (`module_drift`, `corpus_drift`, `pin_corrupt`,
      `certificate_changed`, `certificate_unverified`, `pin_stale`, `*_unreadable`); a
      missing pin, corpus or certificate is a reason, never 'assume fine'. `write` refuses
      to overwrite without `--replace`, because moving a pin changes which parser the live
      lane trusts. Exit codes are the contract: write 0/2, verify 0/1/2. See
      `docs/PARSER_PIN.md`.
- [x] CLI surfaces: `scorpion-review`, `scorpion-certify` and `scorpion-pin`

Still requires environment/operator work:

- [ ] make GitHub repository private (repository-admin setting)
- [ ] purge sensitive values from existing Git history
- [ ] provision Discord bot/application token with minimum intents/permissions
- [ ] reconstruct raw historical Discord message-by-message corpus
- [ ] validate parser precision/recall on that raw corpus
- [ ] shadow run through live sessions before any operational reliance
- [ ] external reviewed brokerage integration, if desired, behind human authorization

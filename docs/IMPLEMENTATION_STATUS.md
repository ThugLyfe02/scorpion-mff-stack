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
- [x] CLI surfaces: `scorpion-review` and `scorpion-certify`

Still requires environment/operator work:

- [ ] make GitHub repository private (repository-admin setting)
- [ ] purge sensitive values from existing Git history
- [ ] provision Discord bot/application token with minimum intents/permissions
- [ ] reconstruct raw historical Discord message-by-message corpus
- [ ] validate parser precision/recall on that raw corpus
- [ ] shadow run through live sessions before any operational reliance
- [ ] external reviewed brokerage integration, if desired, behind human authorization

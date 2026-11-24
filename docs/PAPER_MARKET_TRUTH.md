# Options paper market truth (P0)

This is a capture-driven experiment on the existing PR #8 parser/listener/engine and
confirmed-execution book. It has no live switch and does not authenticate to a vendor.

```sh
python -m scorpion_mff.paper_capture --input /private/capture.ndjson \
  --output /private/runs/session-001 --source authorized-normalizer --author AUTHOR_ID
```

Input is newline-delimited JSON with contiguous integer seq (starting 1), received_ts
(Unix seconds), kind:
- quote: canonical 21-character OSI, event_ts, bid, ask, bid_size, ask_size (contracts).
- signal: message_id, author_id, channel (existing allowed name), text, source_ts.
- gap: clear current quotes; retain watermarks.
- tick: exercise existing expiry checks and clock-driven exits.

One ET day per capture. The normalizer must preserve exchange and receive timestamps,
explicit feed gaps and authorized signal provenance; this runner does not certify that
the supplied source is authentic or licensed. Use synthetic data for CI only. Never
commit vendor captures or private alerts. A new run must use a new directory outside
the repo; aborted runs have no completion receipt. It can consume stdin, but is a
capture-replay clock, not a wall-clock live supervisor.

Fill model: only marketable limits inside regular hours, fresh canonical contracts,
sufficient displayed size, available confirmed simulated inventory and unique order
identity. Fill at the pessimistic limit, not the better displayed touch; consume
displayed size once per accepted quote. No passive fills, partial-fill simulation,
queue position, exchange matching, fees, actual venue fills or overnight settlement.
Rejecting partial size is intentionally conservative and is not a claim that the
remaining liquidity could not have filled. Strategy semantics and the legacy test
broker stay unchanged. Outputs are modeled evidence, not live TCA or certified edge.

Only the current 2025/2026 calendar in the existing engine is supported. The next
slice must bind to a licensed feed, confirm market-status/halts and adjusted-contract
definitions, model explicit fees, persist capture watermarks, exercise crash recovery,
and compare to an independent paper venue. No real orders are authorized.

## Prospective evidence profile (follow-up)

The compatibility capture remains available, but it cannot qualify a prospective modeled sample.
Use the explicit strict profile with `--require-market-status --fee-model PATH
--max-signal-age SECONDS`. The normalized market_status event carries osi, event_ts and status
(TRADING/HALTED/CLOSED/UNKNOWN), with the same receive clock and sequence contract. A halt,
withdrawal, conflicting timestamp or gap invalidates quote state; reopening requires a newer
status and a newer quote. No status is inferred from price alone. Status is a declared provider
observation, not authentication, exchange entitlement or proof of tradability in production.

The existing FeeModel accounts for explicitly declared per-order fees. Net realized session P&L
is withheld if fees are missing, there are no fills, the book is open, or reconciliation fails.
The independently maintained simulator acknowledgement and inventory state must agree with the
execution ledger; flat cashflow must agree with its gross P&L. A flat empty session is not a
qualified sample. No data licensing, live fills, real-money readiness or profitable edge is certified.

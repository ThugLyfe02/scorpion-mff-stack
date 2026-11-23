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

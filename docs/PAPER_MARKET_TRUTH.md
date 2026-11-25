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

## External paper comparison (offline, no vendor submission)

The capture summary now binds its modeled ledger bytes and capture end timestamp. Once an
independent, authorized paper venue provides a normalized export, compare without sending orders:

```sh
python -m scorpion_mff.paper_capture reconcile --run /private/runs/session-001 \\
  --venue-export /private/venue.json
```

Export schema: scorpion.paper-venue-export.v1, authority PAPER_ONLY, source (distinct declared
source), input_sha256 (capture identity), session_date, as_of_ts (epoch seconds at or after capture
end, same ET date), complete_order_export=true, orders and positions. Each order must include the
original client_id, canonical 21-character osi, action (buy/sell), requested_qty, limit,
filled_qty, average_price (null for no fill), and state. Positions use the existing snapshot
contract with canonical OSI as contract_key. A vendor mapping must preserve these identities.
Missing, extra, unresolved and mismatched orders are explicit; price cost is computed only for
matched equal positive terminal quantities. Position drift reuses the existing comparator.
Exact parity is a diagnostic, not market truth or deployment approval. Source independence and
export completeness are declared, not authenticated. A vendor adapter and credentials remain open.

## HTTP quote boundary

The generic HTTP quote provider no longer inherits the brokerage token. Set
MFF_QUOTE_TOKEN separately and pin MFF_QUOTE_ORIGIN to the HTTPS origin of the
authorized feed. The template must contain one {key} placeholder, and response
fields (MFF_QUOTE_KEY_PATH and MFF_QUOTE_TS_PATH) must echo the exact contract
key and provide an epoch-seconds or timezone-aware source timestamp. No redirects,
proxy inheritance, receipt-time freshness substitution, crossed prices or stale
cache renewals. Default field paths contract_key and ts are not vendor claims;
configure them only from authenticated vendor documentation. Test transport
injection remains a local fixture seam, not a production bypass.


## Authorized external paper evidence pilot (Alpaca)

The collector uses only GET requests to the fixed https://paper-api.alpaca.markets
/v2/account, /v2/orders and /v2/positions endpoints. Set SCORPION_ALPACA_PAPER_KEY and
SCORPION_ALPACA_PAPER_SECRET locally, use a dedicated options paper account, and provide
its exact account ID. It does not create, replace or cancel orders. It requires a
single-session capture and same-day post-capture collection; rejects a capped (500)
order response rather than guessing completeness, changed readbacks, non-option or
multi-leg/replaced/non-limit orders and unexpected short positions. The original order
and position response bytes and normalized comparison export are preserved privately.
Account response bytes are not saved; account and response digests are retained.

    python -m scorpion_mff.paper_capture collect-alpaca-paper \
      --run /private/capture --out /private/venue-export --account-id PAPER_ACCOUNT_ID

    python -m scorpion_mff.paper_capture reconcile \
      --run /private/capture --venue-export /private/venue-export/normalized.json

Use identical client_order_id values for externally submitted paper orders. Missing
or extra IDs remain discrepancies. Stable double reads are not atomic venue snapshots,
nor independently signed fills; collector/reviewer admission is still required.
This adapter is documented-API-based, not verified with authenticated credentials.

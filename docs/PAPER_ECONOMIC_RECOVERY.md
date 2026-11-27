# Paper economic evidence and anchored recovery

Stacked on the verified PR #15 revision. No order authority, strategy
changes, live endpoint, credential access, deployment, or automatic restart.

## Cost evidence: deterministic offline assembly

The earlier collector supplies the entry/fill artifact and unsigned
normalized outcome. Independently verify and sign it using the existing
outcome workflow. Use an independent market observation source and an
explicit fee statement. Never infer zero fees, manufacture quotes from
bar prices, or choose a quote after the target horizon.

Prepare a separately pinned normalized input of schema:

```json
{
  "schema": "scorpion.paper-cost-source.v1",
  "authority": "RESEARCH_ONLY",
  "account_ref": "PINNED_ACCOUNT_REF",
  "market_source_id": "PINNED_INDEPENDENT_MARKET_SOURCE",
  "journal_sha256": "SEALED_JOURNAL_HASH",
  "outcome_export_sha256": "SIGNED_ENTRY_EXPORT_HASH",
  "fee_source_sha256": "RAW_FEE_STATEMENT_HASH",
  "quote_source_sha256": "RAW_QUOTE_CAPTURE_HASH",
  "observed_through_ts_utc": "2026-11-30T21:06:00+00:00",
  "collected_ts_utc": "2026-11-30T21:09:00+00:00",
  "complete_fee_export": true,
  "fees": [
    {"venue_order_id": "EXTERNAL_ORDER_ID", "fees_usd": "0.30", "rebates_usd": "0.05"}
  ],
  "quotes": [
    {"quote_id": "UNIQUE_EVENT_ID", "ticker": "XYZ", "kind": "QUOTE",
     "event_ts_utc": "2026-11-30T15:04:00+00:00",
     "received_ts_utc": "2026-11-30T15:04:00.050000+00:00",
     "bid": "10.00", "ask": "10.02"}
  ],
  "session_coverage": null
}
```

Values are schema examples, not actual observations. The manifest must be
compiled after the signed entry outcome review; use independently retained
raw statement and quote capture bytes, plus the externally retained hash of
the normalized manifest. The compiler checks all hashes and source identities,
not provider-specific raw-file semantics. The independent collector and
reviewer must verify that the normalized records faithfully and completely
represent the raw sources. A provider-specific raw fee/quote adapter remains
necessary; no market subscription or external fee endpoint is called here.

The fee roster must exactly match all venue entry order IDs, including
zero-fill terminal entries and rejected entries. Explicit charges and
rebates are required even when zero. Exit fees belong in the round-trip
supplement. An independent source may supply GAP or WITHDRAWAL events with
null bid and ask; global feed faults must be represented for each affected
ticker. Preserve source ordering, all relevant events, and receipt timestamps.
No future event or later receipt may be used as a valuation mark.
The latest eligible event at the target must be a valid fresh quote; a gap
or withdrawal blocks fallback until a later valid quote. Selection uses
a per-ticker index and binary search, not repeated full-history scans.
A missing/stale mark or unknown route closure is retained in the draft
receipt with its reason. Zero initial fill and an absent/unobserved chase
are not treated as a fully observed route. This is deliberately conservative.
The cost horizon and freshness are those already pinned in the existing
cost policy, not optimized against outcomes.

The existing full-session coverage object is optional; the operator must
independently attest it. Use null when completeness cannot be substantiated.
The existing Stage 2 verifier still checks its lifecycle, readiness,
calendar and denominator constraints; this compiler never infers coverage
from a local journal receipt.

Use paper-prepare-costs with the existing paper-outcome-labels arguments
(signed entry export and signatures), plus:

```sh
--outcome-draft /private/unsigned-entry.json --outcome-draft-sha256 ENTRY_DRAFT_PIN \
--cost-policy /private/cost-policy.json --cost-policy-sha256 COST_POLICY_PIN \
--cost-source /private/normalized-cost-source.json --cost-source-sha256 SOURCE_PIN \
--fee-source /private/raw-fees --quote-source /private/raw-quotes \
--out /private/unsigned-cost.json
```

The compiler refuses missing fees and writes a mode-0600 exclusive draft.
A draft with withheld marks exits nonzero but retains the diagnostic output.
The normalized draft is intentionally unsigned with reviewed_ts_utc=null.
Independently review, set only the actual review timestamp, and sign the
exact finalized cost export bytes using the existing cost signature domains.
For paper-verify-reviewed-costs use the same arguments except --out; add:

```sh
--cost-draft /private/unsigned-cost.json --cost-draft-sha256 COST_DRAFT_PIN \
--cost-export /private/reviewed-cost.json \
--cost-collector-signature /private/cost-collector.sig \
--cost-reviewer-signature /private/cost-reviewer.sig
```

Recompilation re-verifies the original signed entry evidence and all
source files. The reviewed cost content cannot differ from the pinned
draft except for its review timestamp, even if both signatures are valid.
The existing Stage 2 verifier then evaluates actual signed costs.
Use the normalized cost source as market-artifact for the legacy cost
cohort index, retaining the separately pinned fee and quote originals.
Legacy Stage 2/cohort commands remain compatible but do not themselves
recompile this new draft; the operator must verify and retain the closeout
receipt before admitting the files to future study input indexes.

Economic surveillance additionally checks independently signed entry
charges and rebates against the round-trip fee roster. Matching net fees
do not excuse conflicting gross charges/rebates. Missing or contradictory
fees leave the economic session interval unresolved rather than reporting
a potentially false return.

## Crash forensics

Stop the worker and preserve the original database, WAL, journals and
external evidence. Keep the latest trusted ledger sequence and chain hash
outside execution custody; an opening-only checkpoint does not attest a
later suffix. Use a consistent private copy of the database, not a loose
copy that omits active WAL state. On the offline copy:

```sh
python -m scorpion paper-recovery-audit \
  --ledger /private/paper.db --policy /private/risk.json --policy-sha256 RISK_PIN \
  --anchor-sequence LAST_INDEPENDENT_SEQUENCE --anchor-head LAST_INDEPENDENT_HEAD
```

Optionally add --source-artifact and --source-sha256 for an independently
captured post-close venue artifact from paper-collect-venue. The entire
hash chain and external checkpoint membership are verified in one read-only
database transaction. A missing/mismatched anchor refuses (rollback or
fork possible). A valid unanchored suffix is identified as such, not
certified. Every known local and external order is classified; an absent
external order never authorizes release of an ambiguous reservation.
The report carries the candidate head for human review, not permission to
resume. It makes no network requests, changes no state and cannot clear
a latch. Active recovery still requires the previously documented explicit
operator recovery and cancellation process; no automatic resubmission.

## Remaining

Authentic paper account, licensed quote entitlement, independent credential
custody, host benchmarks and actual signed raw sources remain prerequisites.
This PR does not automate broker fee statements, independent raw quote
collection, exact opening/closing fee-inclusive round-trip drafting,
cross-host external checkpoint storage, streaming venue ingestion or
incremental hot-path journal verification. It provides an offline bridge
and recovery diagnostic; passing tests do not establish real execution
latency, profitability, model improvement, or live readiness.

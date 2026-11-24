# Paper intent and evidence integrity gate

The forward equity service previously used a test double that returned PAPER_FILLED
even for resting limits and exits without a price. It now records PAPER_INTENT_ONLY,
separates requested limit from fill price, and labels chase and exit alternatives
conditional. The legacy replay broker and frozen forward strategy remain unchanged.

The journal is bounded, fails visibly on saturation or writer/fsync failure,
and never silently restarts into an existing session file. Close proves drain;
asynchronous decision timing is distinct from durable ack timing. A proper
resume protocol must restore working calls and watermarks before same-file reuse.

Discord ingress shares supervision with the session. Unexpected feed/ingress
termination is a session fault; an explicit author allowlist is required for the
Discord-enabled service. No real credentials or order submission are involved.

Remaining: causal equity fill-state simulation, fees, real licensed feed
validation, complete restart recovery, and external paper comparison. This
change does not claim to fill or certify trades.


## Follow-up: immutable paper evidence and explicit admission

The existing JSONL writer now freezes records at admission and binds its own byte digest,
record count and sequence to a sidecar receipt only after fsync, close and independent readback.
A missing, torn, changed or reordered journal cannot verify. The receipt is local evidence,
not a signature or proof of source authenticity. Use:

```sh
python -m scorpion verify-paper-journal --journal /private/session.jsonl
```

The forward service records content/revision hashes and source/receive timestamps, the
locked rules hash and the quote actually used for the intent. It rejects foreign guilds,
noncausal messages and unauthorized authors. Exact creates are deduplicated, and a conflicting
create trips the existing latch. Revisions retain the existing ignore policy. Bounded Discord
queue overflow explicitly reaches supervision instead of relying on callback exceptions.

A valid receipt establishes journal closure and byte identity only. No fill or profitability
is asserted. Raw private messages and market captures must remain outside Git. Crash recovery
and provider/venue authentication remain external prerequisites.

## Crash recovery checkpoint

The writer now fsyncs the journal batch, atomically publishes its verified-prefix checkpoint,
then issues durable callbacks. Directory entries are fsync'd on the POSIX/systemd deployment;
other platforms do not claim equivalent power-loss directory durability. The sidecar is not a
clean-close receipt or authenticated signature. Offline recovery requires the writer to be stopped:

```sh
python -m scorpion recover-paper-journal --journal /private/session.jsonl --out /private/recovery
```

The verified prefix and any unverified trailing bytes are preserved separately, with source and
checkpoint digests; the original file is untouched. A missing or mismatched checkpoint refuses
rather than guessing which events were durable. No recovered file is represented as a clean run.
This is evidence recovery, not automatic working-call resume or external execution recovery.


## Point-in-time research features and conditional alternatives

Use the existing causal_features and counterfactual modules to extract a sealed journal's
unlabeled research features or compare its recorded initial and conditional-chase intentions:

```sh
python -m scorpion paper-intent-features --journal /private/session.jsonl
python -m scorpion paper-intent-alternatives --journal /private/session.jsonl
```

Every accepted row binds the journal bytes, earlier decision sequence, message revision and
content fingerprints, rules hash, quote event clock, receive-to-decision age, limit and quantity.
Missing/noncausal evidence is refused, not imputed. The two alternatives are paired by call ID;
only requested-limit, quantity and timing differences are measurable. No execution or P&L delta
is imputed. No row is a labeled TrainingRow and no optimizer is invoked. Authenticated venue or
adjudicated outcome evidence and independent qualification remain prerequisites for learning.


## P1 — independently attested paper outcome labels

Use the existing execution_meta_labels module with the existing sealed journal feature extractor.
This new boundary is offline. It has no order submission, automatic training, or promotion authority.
Install the optional paper-evidence extra for Ed25519 verification.

The operator pins the exact SHA-256 of an independently maintained trust JSON file before
collection. Schema scorpion.paper-outcome-trust.v1, authority PAPER_ONLY, source_id, account_ref
(use an opaque paper-account reference), rules_sha256, registered_ts_utc, horizon_seconds,
collector_id, collector_public_key, reviewer_id, reviewer_public_key. Public keys are 32-byte
lowercase hex Ed25519 keys. Different IDs and keys are mandatory; separate custody must be
enforced outside this code. The declared registration timestamp is not a trusted timestamp proof.

The external collector maps an original paper-venue export to scorpion.paper-outcome-export.v1:
authority PAPER_ONLY, source_id, account_ref, trust_sha256, journal_sha256,
source_artifact_sha256, complete_order_export=true, observed_through_ts_utc, collected_ts_utc,
reviewed_ts_utc, orders. Each order contains feature_sha256, venue_order_id, call_id, ticker,
role (INITIAL or CHASE), intent_seq, requested_quantity, limit (decimal string), state,
asset_class=EQUITY, side=BUY, currency=USD, quantity_unit=SHARES,
submitted_ts_utc, terminal_ts_utc (null for unresolved), fills (fill_id, quantity, price
as decimal string, filled_ts_utc). Timestamps must be timezone-aware. The original vendor
artifact is provided separately and hashed. Both parties sign the exact normalized export bytes,
collector with prefix b"scorpion.paper-outcome-export.v1:collector\x00" and reviewer with
b"scorpion.paper-outcome-export.v1:reviewer\x00" (actual NUL byte); signatures are detached
lowercase hex (64 bytes). No private signing key belongs in Scorpion or Git.

```sh
python -m scorpion paper-outcome-labels --journal /private/session.jsonl \
  --trust /private/trust.json --trust-sha256 PREAPPROVED_SHA256 \
  --export /private/normalized.json --source-artifact /private/vendor-export.json \
  --collector-signature /private/collector.sig --reviewer-signature /private/reviewer.sig \
  --as-of 2026-11-24T21:00:00+00:00
```

Missing orders stay UNOBSERVED, unresolved or horizon-incomplete orders CENSORED, explicit
terminal cancellations/expiry with zero fill are distinct from rejection. A rejection is not
a zero-liquidity target. Conflicting identity, limit, quantity or chronology is quarantined.
Duplicate venue order/fill IDs refuse the export. Chases must be supported by an earlier
terminal zero-fill initial order, never double counted as a second executed alternative.
The exact evaluation cutoff limits available evidence. All report labels remain RESEARCH_ONLY;
no P&L or live fill is inferred. Signatures bind attestation, not vendor truth: authenticated
provider collection, independently administered keys, external account isolation, preregistered
horizons, dataset-wide deduplication and independent model qualification remain prerequisites.
The current legacy forensic labels are unchanged; these are a separate paper evidence class,
not a replacement for accepted human labels or the existing execution ledger.


## P1 stage 2 — paper entry implementation shortfall and conditional chase attribution

This extends the existing execution_attribution and counterfactual modules, not a parallel
simulator. The Stage 1 signed outcome export is reverified on every cost report. The new
cost-policy file must be separately pinned before observations. Schema:
scorpion.paper-cost-policy.v1, authority RESEARCH_ONLY, trust_sha256, registered_ts_utc,
horizon_seconds, max_mark_age_ms (<=3000), market_source_id (distinct from outcome source),
benchmark INITIAL_DECISION_MID, fee_scope ALL_ORDER_CHARGES_AND_REBATES_USD.

A separately dual-attested normalized supplement has schema scorpion.paper-cost-export.v1,
authority PAPER_ONLY, trust_sha256, cost_policy_sha256, journal_sha256, outcome_export_sha256,
market_source_id, market_artifact_sha256, complete_fee_export=true, observed_through_ts_utc,
collected_ts_utc, reviewed_ts_utc, fees and marks arrays. Fees have feature_sha256,
venue_order_id, fees_usd, rebates_usd (nonnegative decimal strings, explicit zero required).
Marks have call_id, initial_feature_sha256, ticker, quote_id, target_ts_utc, event_ts_utc,
received_ts_utc, bid, ask, route_closed (boolean). A mark must be available by the fixed
parent horizon, fresh, uncrossed, and bound to the original market artifact; that artifact
must differ from the outcome artifact and normalized exports. Detached signatures use
the same pinned collector/reviewer identities but domains
b"scorpion.paper-cost-export.v1:collector\x00" and b"scorpion.paper-cost-export.v1:reviewer\x00"
(actual NUL). Fee mapping and route closure are signed declarations, not independently
authenticated vendor facts. The operator must preserve source bytes, independent key custody,
trusted timestamping and pre-registration outside this process.

```sh
python -m scorpion paper-execution-costs --journal /private/session.jsonl \
  --trust /private/trust.json --trust-sha256 TRUST_PIN \
  --export /private/outcomes.json --source-artifact /private/venue.json \
  --collector-signature /private/outcome-collector.sig \
  --reviewer-signature /private/outcome-reviewer.sig --as-of CUTOFF \
  --cost-policy /private/cost-policy.json --cost-policy-sha256 COST_PIN \
  --cost-export /private/costs.json --market-artifact /private/market.json \
  --cost-collector-signature /private/cost-collector.sig \
  --cost-reviewer-signature /private/cost-reviewer.sig
```

For buy entries, original parent Q and initial decision midpoint B are fixed.
For each filled leg i, q_i is observed quantity, N_i exact fill notional and M_i
the sealed leg midpoint. With horizon midpoint H and net fees F:
timing = sum(q_i*(M_i-B)); within-leg slippage = sum(N_i-q_i*M_i);
opportunity = (Q-sum(q_i))*(H-B); shortfall = timing + slippage + F + opportunity.
Positive shortfall is cost, negative is price improvement; basis points use Q*B.
The decomposition must equal sum(N_i)-sum(q_i)*B+F+opportunity exactly.
Order-level gross slippage remains descriptive if fees are absent; route net
shortfall is withheld if any required outcome, fee, mark, horizon or route closure is missing.
Partial fills keep the original Q; a reduced chase target never silently erases opportunity cost.
The predeclared horizon must fit within the same regular session, including early closes.
Stage 1 feature hashes use canonical decimal precision; costs use a fixed 50-digit context.

For an observed terminal chase following an attested terminal zero-fill initial order,
the no-chase reference retains the initial fees and marks all Q unfilled at the same H.
The difference is the observed chase notional minus chase quantity times H plus chase
net fees. This is a path-conditional reference, not an estimated counterfactual fill,
causal policy value, liquidation price, realized P&L or live execution. The report never
authorizes training/promotion and must not feed signed shortfall into the existing
nonnegative-cost policy-value evaluator as if it were independent net reward.
Session clustering, prospective experimental design, dataset-wide deduplication,
exit/round-trip economics and promotion gates remain separate stages. No real orders.


## P1 stage 3 — fixed prospective session cohorts and robust uncertainty

Stage 3 uses the existing Stage 2 signed cost admission, block bootstrap, confidence sequence,
regime bucketing, empirical-Bayes shrinkage and bounded mixture solver. A new offline
orchestrator is not an alternative execution engine. No runtime optimizer or approval path
consumes these outputs.

### Complete-session evidence

The existing paper runtime now journals session_start and session_end (only after successful
supervision). Both bind session date, mode, rules hash and UTC timestamp. The start records
whether signal ingress is enabled; the end records feed messages/disconnects, ingress overflow
and pending queue size. A clean journal receipt without these markers is not a complete session.
A Stage 2 cost export may optionally carry the following separately signed member (old exports
remain valid for Stage 2 but cannot support Stage 3):

    "session_coverage": {
      "session_date": "2026-11-24",
      "covered_from_ts_utc": "2026-11-24T14:30:00+00:00",
      "covered_through_ts_utc": "2026-11-24T21:00:00+00:00",
      "expected_initial_routes": 12,
      "journal_complete": true,
      "ingress_complete": true,
      "no_feed_gaps": true
    }

Both Stage 1 and Stage 2 observed-through clocks must cover the end marker.
The declaration is checked against the calendar, sealed journal, actual initial-intent count,
transport counters, rules hash and recorded routes. A latch observation, late start, early end,
disconnect or overflow makes the session uncertain, not empty. This is still an attestation:
it cannot prove vendor feed completeness or hidden signal omissions. Historical journals without
lifecycle markers must remain unknown rather than being retroactively certified.

### Preregistered cohort

Before the first scheduled regular session, an operator external to the optimizer pins the exact
SHA-256 of a JSON file with the following fields (example thresholds are engineering defaults,
not claims of adequate statistical power):

    {
      "schema": "scorpion.paper-session-cohort.v1",
      "analysis_version": "paper-session-statistics-v1",
      "authority": "RESEARCH_ONLY",
      "family_id": "paper-entry-cost-family-001",
      "registered_ts_utc": "2026-11-20T20:00:00+00:00",
      "start_date": "2026-11-23",
      "end_date": "2026-11-27",
      "sessions": ["2026-11-23","2026-11-24","2026-11-25","2026-11-27"],
      "trust_sha256": "PINNED_STAGE_1_HASH",
      "cost_policy_sha256": "PINNED_STAGE_2_HASH",
      "family_alpha": 0.05,
      "clip_bps": 500,
      "minimum_sessions": 4,
      "minimum_routes": 100,
      "minimum_complete_fraction": 0.95,
      "minimum_effective_sessions": 3,
      "maximum_session_share": 0.4,
      "maximum_ticker_share": 0.5,
      "minimum_primary_score_bps": 0,
      "minimum_chase_score_bps": 0,
      "minimum_observed_chases": 30,
      "maximum_tail_loss_bps": 100,
      "minimum_group_sessions": 2,
      "density_ratio_bound": 2,
      "block_days": 2,
      "bootstrap_trials": 2000,
      "random_seed": 170053
    }

Replace example pins with real lowercase 64-character digests. For actual studies use a longer
predeclared horizon and power analysis. All exchange sessions in the date range must be listed,
including early closes but excluding holidays. Trust and cost policy registration must precede
cohort registration, which must precede the first open. The declared registration time and pin
require external trusted custody; the code cannot prove that the operator did not backdate them.

The input index is separate from the cohort pin. It is an object with schema
scorpion.paper-session-inputs.v1 and a sessions array. Each row has session_date and
paths (relative to the index directory or absolute): journal, outcome_export, source_artifact,
collector_signature, reviewer_signature, cost_export, market_artifact,
cost_collector_signature, cost_reviewer_signature. There is at most one bundle per elapsed
session; future or duplicate dates refuse. The original Stage 1 and 2 files are reverified
under the pinned policies and cutoff. No arbitrary numeric result is admitted. Duplicate
journal/artifact hashes, call identities, signal revisions, venue order IDs or fill IDs across
sessions refuse the cohort. IDs must be globally scoped to the pinned paper account.

    python -m scorpion paper-session-statistics \
      --cohort /private/cohort.json --cohort-sha256 COHORT_PIN \
      --inputs /private/session-inputs.json --trust /private/trust.json \
      --cost-policy /private/cost-policy.json --as-of 2026-11-28T01:00:00+00:00

### Estimands, missingness, and confidence

Two fixed endpoints (higher is better), both over original initial intent routes:
1. Negative Stage 2 implementation shortfall in basis points.
2. Negative observed-minus-no-chase shortfall in basis points, zero only for a complete,
   attested route with no chase submitted. This is observed-path accounting, not causal
   challenger value. A censored chase remains unknown; actual chase intervention counts
   are separately reported.

The bounded estimand is the equal-session average of route scores clipped to [-B,B] under the
preregistered B. A fully attested session with no initial intents has structural zero activity.
A missing/invalid session gets [-B,B]; if a known denominator has m unknown routes and
sum s of observed clipped scores, the day's interval is [(s-mB)/N,(s+mB)/N].
This bounds the clipped estimand, not raw unbounded costs. There is no missing-at-random
assumption. No trade-level pseudo-replication: one interval per elapsed exchange session.

Hoeffding with alpha_n=alpha_endpoint/(n(n+1)) produces simultaneous over-time bounds for
the average predictable conditional means of the bounded session scores under the declared
filtration and valid intervals. Family alpha is split equally between the two fixed endpoints
(Bonferroni), not repeatedly spent on arbitrary subgroup searches. No stationary-future-mean,
IID-session or live-alpha claim is made. This is a within-cohort mathematical bound; preventing
new-cohort alpha resets requires independent registry/ledger governance. Inputs can be
re-evaluated as evidence arrives, with the same pinned roster and cutoff; reports are hashed.

Complete-case raw session averages, route averages, leave-one-session-out range, worst route,
worst-decile route mean and lag-one correlation are diagnostics. Circular block bootstrap uses
predeclared seed/trials and block lengths 1, configured, and twice configured (capped at 30);
it is withheld when sessions are incomplete or samples degenerate, and never called
anytime-valid. Kish effective session count measures route-count concentration, not serial
dependence-adjusted inferential degrees of freedom. Raw extremes are preserved; any clipping
blocks positive support status. Raw tail, route coverage, session diversity, session/ticker
concentration and absolute ticker score concentration are independent blockers.

### Regime diagnostics

Equity bands are fixed in v1: spread <=10/50 bps; quote age <=100/1000 ms; source-to-intent
age <=120/300 seconds; existing open/midday/close session buckets. They use pre-intent
features, not outcomes. Every observed group retains its denominator and distinct sessions.
Empirical-Bayes means reuse the existing moment estimator on group/session means and are
descriptive only (groups can overlap in dates). Mixture stress reuses the exact bounded
density-ratio solver on raw route-weighted group means, not the equal-session primary
estimand. It is withheld if any bucket is unobserved/under-supported or coverage is missing.
Axes are marginal, not joint macro regimes. No subgroup significance or discovery claim.

A positive bounded-support status is research evidence only, not a signal to retrain or
promote. The CLI returns nonzero for blocked/insufficient evidence while printing the report.
Prospective independent qualification, global hypothesis-budget governance, regime shift
validation on fresh data, authentic live market feed, signed venue adapter, and complete
round-trip P&L remain separate prerequisites.

Stage 2 cost-report hashes now use schema scorpion.paper-execution-cost-report.v2 to bind the
new coverage, identities and regime fields. Prior v1 reports remain reproducible at their old
commit; they are not silently reinterpreted as complete-session evidence. Terminal chase
counts include attested zero-fill cancellations, not just orders that actually acquired shares.


The session lifecycle additionally requires an armed safety latch at both boundaries, an
authorized Discord guild/channel cache ready by the opening bell, no pre-close disconnect
or callback failure, and a first usable admitted quote by the opening bell. This is conservative
telemetry, not proof of vendor entitlement or complete history. No readiness event changes
trading permission. A genuinely empty session still requires all the evidence.

Chase-specific Kish diversity, per-session/ticker counts and absolute score shares are
computed using only attested terminal chase interventions; structural no-chase zeros cannot
inflate their effective support. Primary and chase raw session-tail checks are independent.
The planned-horizon radius and best possible lower bound reveal underpowered cohort designs
without tuning the support or stopping threshold after seeing data. These are best-case
mathematical resolution diagnostics, not empirical power forecasts.

Both lifecycle markers include the existing safety ledger's verified event count. A changed
count or invalid ledger blocks full-session evidence even if the latch was rearmed by close.
This adds two off-hot-path integrity reads; no latch state is changed by the research logic.

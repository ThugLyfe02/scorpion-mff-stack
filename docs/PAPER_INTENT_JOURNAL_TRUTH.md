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


## P1 stages 3+ and 4 — prospective research qualification

The existing hypothesis-memory ledger now stores signed reservations and one-shot finalizations
on the same hash chain. Alpha is reserved in integer billionths **before** the first session;
the lifetime prospective budget is 50,000,000 units (0.05). No refund on rejection, no overlap
of exchange dates even across family aliases, one root per registry, and no replay of finalized
evidence identities. This is intentionally conservative: concurrent independent studies must
use nonoverlapping rosters or be treated as one paired trial, not create extra registry roots.
Legacy exploratory hypothesis resolutions are not counted in this new prospective budget.
A separately administered, latest external checkpoint is mandatory: a local SQLite hash chain
alone cannot prevent restoring an older valid database or cloning a fresh ledger. The operator
must retain the latest chain hash outside the research/optimizer process. The CLI never signs.

The root file (externally pinned exact SHA-256) has schema scorpion.prospective-study-root.v1,
authority RESEARCH_ONLY, registry_id, registered_ts_utc, registrar_id, registrar_public_key,
reviewer_id, reviewer_public_key. Keys are lowercase raw Ed25519 32-byte hex. The registration
and review keys must differ from one another and from the arm collector/reviewer IDs and keys.
Separate custody and trusted clock/registration are operational requirements, not established
merely by distinct public keys.

The pinned study JSON has schema scorpion.prospective-study.v1, authority RESEARCH_ONLY,
family_id, kind (COHORT or PAIRED_ENTRY_TRIAL), registered_ts_utc, alpha_units,
embargo_seconds (at least 86400), minimum_improvement_bps, maximum_challenger_cost_bps,
soak_sessions, adverse_session_null_rate, adverse_session_alternative_rate, upstream_source_id,
and arms. The upstream source declaration must differ from both venue and market source IDs
in a paired trial.
Arm order is COHORT or INCUMBENT then CHALLENGER. Each arm contains arm, cohort_sha256,
artifact_sha256, code_revision (40 lowercase hex), training_manifest_sha256, frozen_ts_utc.
The exact input index uses schema scorpion.prospective-study-inputs.v1 and arms, each with
arm, cohort, trust, cost_policy, artifact, training_manifest, sessions (paths relative to index).
Paired studies additionally provide rosters, an array of session_date, roster, source_artifact,
and signature paths. Empty before collection; missing elapsed rosters remain unknown.
The arm cohort and trust/fee-policy bytes are reverified. In a pair, account references and
frozen rules/artifact hashes must be different; source venue, market source, date roster,
benchmark/observation horizons and evaluation thresholds must agree.
The training lineage file scorpion.frozen-training-lineage.v2, authority RESEARCH_ONLY,
contains artifact_sha256, code_revision, rules_sha256, trained_through_ts_utc,
labels_available_through_ts_utc, frozen_ts_utc, training_data_sha256 (nonempty digest list),
artifact_kind (DETERMINISTIC_RULES or TRAINED_MODEL), and walk_forward_folds. Each declared fold
contains train_through_ts_utc, training_labels_available_through_ts_utc, validation_start_ts_utc,
validation_end_ts_utc, validation_labels_available_through_ts_utc, training_data_sha256 and
validation_data_sha256. Trained models require at least three declared folds; deterministic
rules may use an empty fold list. The existing walk_forward module verifies nonoverlapping
validation windows, purged label availability and embargo, no future validation data in training,
no duplicate validation identities, and inclusion in the frozen data inventory. Earlier holdouts
can be used in later training only after their labels are available.
Actual artifact bytes and lineage bytes are pinned; label availability and freeze must precede
the holdout with the embargo. This checks declared lineage, not reconstructing model training.
No training/holdout data artifact hash overlap is allowed.

The Stage 2 optional signed trial_binding has study_sha256, arm, artifact_sha256,
code_revision, common_roster_sha256, execution_manifest_sha256. These fields are checked
against the pinned plan and canonical common roster, including signal revision, ticker,
source/initial clocks, initial target, benchmark, horizon and initial quote identity.
Both arms must match the independently signed common opportunity roster and each other, with
identical market artifact bytes for a session; distinct local journals and venue exports/artifacts;
equal horizon midpoint and quote ID for a matched route. The independent roster is signed by
the registrar, not an arm collector or final reviewer, over the exact JSON bytes prefixed by
b"scorpion.common-entry-roster.v1\x00" (actual NUL). Schema scorpion.common-entry-roster.v1,
authority RESEARCH_ONLY, study_sha256, session_date, source_id, source_artifact_sha256,
complete_roster=true, observed_through_ts_utc, signed_ts_utc, routes. Each route carries
revision_id, ticker, source_ts_utc, initial_ts_utc, target_quantity, decision_midpoint (decimal
string), horizon_ts_utc, initial_spread_bps (decimal string), initial_quote_event_ns. Source bytes,
clock bounds and independent signature are verified; a missing or mismatched roster withholds
the session. Roster artifact and signal identities are included in cross-study reuse protection.
This attests a declared upstream population, not independently proven live capture completeness. The execution_manifest_sha256 is the canonical declaration
digest from execution_binding_digest, not a signed remote runtime measurement. The code
cannot create or run the second strategy or establish independent vendor evidence.
Selection/abstention outside the common initial-intent population is not evaluated.
No unmatched route is counted as zero. No synthetic order/fee/return is manufactured.
Stage 2 report schema is now v3; historical v2 reports remain reproducible at prior commits.

Approvals are detached lowercase hex Ed25519 signatures over exact JSON bytes, domain
b"scorpion.prospective-study-approval.v1:reserve\x00" or
b"scorpion.prospective-study-approval.v1:review\x00" (actual NUL).
Common approval schema scorpion.prospective-study-approval.v1, authority RESEARCH_ONLY,
action RESERVE/REVIEW, root_sha256, study_sha256, ledger_chain_hash, signer_id,
issued_ts_utc, expires_ts_utc (maximum 7 days). Review adds dossier_sha256 and disposition
(PAPER_RESEARCH_REVIEWED, INCONCLUSIVE or REJECTED). Reserve uses registrar; final review uses
independent reviewer. Registration, ledger pin, signatures and actual UTC clock checked;
finalization recomputes the original evidence at the exact reviewed cutoff, so changed inputs
cannot reuse the old approval. Both approval bytes/signatures and final canonical dossier
are retained in append-only records. No signature authorizes a model, runtime or brokerage.

    python -m scorpion paper-study-reserve --registry /private/hypotheses.db \
      --root /private/root.json --root-sha256 ROOT_PIN --study /private/study.json \
      --study-sha256 STUDY_PIN --inputs /private/study-inputs.json \
      --ledger-chain-sha256 CURRENT_EXTERNALLY_PINNED_CHAIN \
      --approval /private/reserve.json --signature /private/reserve.sig

    python -m scorpion paper-study-assess --registry /private/hypotheses.db \
      --root /private/root.json --root-sha256 ROOT_PIN --study /private/study.json \
      --study-sha256 STUDY_PIN --inputs /private/study-inputs.json \
      --ledger-chain-sha256 CURRENT_EXTERNALLY_PINNED_CHAIN --as-of CUTOFF

    python -m scorpion paper-study-finalize --registry /private/hypotheses.db \
      --root /private/root.json --root-sha256 ROOT_PIN --study /private/study.json \
      --study-sha256 STUDY_PIN --inputs /private/study-inputs.json \
      --ledger-chain-sha256 CURRENT_EXTERNALLY_PINNED_CHAIN --as-of CUTOFF \
      --approval /private/review.json --signature /private/review.sig

Status and dossier inspection: paper-study-status or paper-study-dossier, --registry,
--root-sha256, --ledger-chain-sha256 (dossier additionally --study-sha256).
After a write independently retain the returned new chain hash; before another write
verify against that pin. Genesis is 64 zeros for an empty ledger. Do not reset or fork.
The prior paper-session-statistics command remains an unreserved diagnostic only.

Paired entry improvement = incumbent shortfall minus challenger shortfall (higher is better).
The primary endpoint reuses the session-bounded inference and concentration diagnostics,
with 80% of the reserved alpha. The fixed adverse-session Bernoulli e-process receives 20%;
the null is a bounded predictable conditional adverse-session probability. Unknown sessions
supply lower indicators only; no alarm is not proof of health. A final dossier requires all
planned sessions elapsed, complete paired coverage, independent session diversity, raw tail
and absolute challenger-cost guards, robust observed marginal regimes, positive sequential
lower bound, and recent fixed-window soak performance. An alarm blocks readiness.
The arm reports are exploratory diagnostics only within a paired study, not separately
confirmatory hypotheses with extra alpha. This is not a randomized causal strategy trial,
portfolio P&L, or whole-strategy qualification: exits, independently observed round-trip
economics, allocation/risk, authentic market data, strategy deployment and production approval
remain outside this boundary. The only positive disposition is paper research reviewed.


### Production-readiness fingerprint maintenance

The previous preparation fingerprint included exact physical main-database and WAL byte sizes.
A SQLite checkpoint can move already committed pages between the two without changing logical
control state. The v2 preparation-state fingerprint now omits only these two physical-placement
counters, preserving logical page count/freelist ratio, all observed risk findings (including
WAL growth), health, backlog, release identity, safety epoch and latency evidence. The raw
audit hash still binds both physical sizes, and the existing operator READY requirement and
control-state compare-and-swap remain unchanged. Old preparation certificates must be reissued
under the new fingerprint/policy version. Read-only audit connections are explicitly closed,
and use read transactions to reduce inconsistent intra-audit observations. The regression tests
exercise a real WAL checkpoint plus a changed queue and a breached WAL threshold.


## P0 operational capture and conservative equity depth replay

The actual run-session service now checks regular-session date, minute-rounded entry,
timer lateness (3 seconds), and an early-close-aware exit cap. The historical frozen
OHLC evaluator and golden CSV remain unchanged. Strict scheduling is a prospective
wall-clock approximation, not an assertion of historical fill parity.

Explicitly enable bounded market capture with:
    
    python -m scorpion run-session --capture-market

Use authorized Discord bot credentials/author IDs and a licensed Databento subscription.
No real orders are sent. The existing journal captures admitted top-of-book observations
only for symbols with a recorded intent through scheduled exit + 60s, plus global gaps.
At 80,000 market records or writer backpressure, supervision refuses rather than silently
sampling. This is an intentionally bounded pilot, not a full-session market archive.
The XNAS.ITCH MBP-1 book is single-venue, not NBBO, and continuity is not independently proven.

    python -m scorpion paper-market-replay --journal /private/session.jsonl

The replay requires a verified journal, strict schedule and valid intent features, exact
quote event and receipt clocks, sizes and instrument identities. It never executes a fill
from a quote before its intent. Initial modeled buys require ask through the frozen threshold;
chase uses an immediately marketable quote during its first minute and is suppressed if
any initial quantity filled. A shared per-symbol budget consumes displayed depth; at unchanged
prices only increases in displayed size add liquidity. Exit models bid-side execution only
after the recorded conditional exit, for actual modeled inventory, within a 60-second
window. Partial fills preserve residual inventory. Gaps/withdrawals halt further simulated
fills; unresolved positions, missing coverage, late readiness, and incomplete lifecycle
withhold aggregate net round-trip economics. The frozen round-trip bps is a modeled cost
assumption, not observed fees. Quotes do not prove execution/queue priority, market status
or complete tape capture. The reports are never accepted as Stage 1 outcome labels.

    python -m scorpion recover-paper-journal --journal /private/session.jsonl --out /private/recovery
    python -m scorpion paper-market-replay --recovered /private/recovery

Recovered prefixes revalidate the checkpoint, prefix, unverified tail and source hashes.
They reconstruct modeled inventory as of the durable prefix, retain unresolved quantities,
and always withhold aggregate qualification. No automatic resume, broker action or latch
clear is authorized. Provider authentication, real tape continuity, load testing and actual
venue reconciliation remain operational prerequisites.


### Live entitlement probe

Historical metadata access is not proof of live entitlement. During the regular session,
an operator may explicitly authorize a bounded subscription:

    python -m scorpion paper-feed-probe --symbol SPY --timeout 20 --authorize-subscription

This may incur market-data subscription charges according to the account's contract.
It requires a fresh, uncrossed quote with positive displayed depth and instrument identity
from the official adapter, closes the stream, and returns a sanitized result. No orders
or continuity certification. Do not put credentials in arguments, chat, Git or logs.
SCORPION_BROKER_MODE is checked again at runtime; REVIEW_ONLY cannot enable market capture.


## P0/P1 — loss-intolerant capture and external round-trip reconciliation

The supervised runtime uses Journal(require_lossless=True). Admitted bytes are bounded by
the same 64 MiB verified-reader limit, single records by 256 KiB and admitted-but-not-yet-
checkpointed bytes by 8 MiB. These are conservative pilot limits, not throughput claims.
A rejected record permanently poisons that strict writer; close drains admitted evidence
but emits no clean receipt. The durable-prefix recovery procedure is unchanged. Readers
reject duplicate JSON keys and nonfinite numbers even if the local unkeyed digest matches.

The existing reconciliation module now admits a distinct signed supplement, using the
Stage 1 trust pin and collector/reviewer keys. The normalized schema scorpion.paper-round-trip.v1
contains authority PAPER_ONLY, source_id, account_ref, trust_sha256, journal_sha256,
outcome_export_sha256, source_artifact_sha256, session_date, opening_ts_utc, closing_ts_utc,
observed_through_ts_utc, collected_ts_utc, reviewed_ts_utc, complete_account_activity=true,
no_nontrade_cash_movements=true, opening_cash_usd, closing_cash_usd, opening_positions,
closing_positions, suppressed_chase_feature_ids, exit_orders, fees. Use USD *ledger cash*
(not buying power), an isolated opening-flat account, and the original session's signed
entry evidence. Snapshots must bracket the regular session. Positions are ticker and
positive integral quantity rows. The original separate account/exit artifact is hashed.
No venue authentication is inferred from two signatures or from exact arithmetic.

Each exit order has call_id, ticker, intent_seq (conditional exit record), venue_order_id,
requested_quantity, state, submitted_ts_utc, terminal_ts_utc (null if unresolved), limit
(decimal string or null for a market exit), fills (same per-fill schema as Stage 1),
asset_class=EQUITY, side=SELL, currency=USD, quantity_unit=SHARES. Only one exit order
per call is supported; replacement chains and multiple liquidation orders are not silently
merged. The requested quantity must equal independently observed entry inventory and not
exceed the conditional intent's quantity. Entry and exit windows, terminal timestamps,
sell limit compliance and duplicate order/fill identities are checked. A known initial
positive fill can suppress an otherwise unobserved conditional chase only by explicit
feature-hash attestation. Missing/rejected/censored and partial exits remain distinct.
Each fee row is venue_order_id, fees_usd, rebates_usd (explicit zero allowed).

Collector/reviewer sign exact supplement bytes with distinct domains
b"scorpion.paper-round-trip.v1:collector\x00" and b"scorpion.paper-round-trip.v1:reviewer\x00"
(actual NUL). All original signed entry evidence is reverified. For the supported flat account:

    reconstructed_cash = opening_cash - sum(entry notionals)
                         + sum(exit notionals) - sum(net fees)

Reported closing cash must match exactly, and reconstructed remaining shares must match
the independently reported closing snapshot. Nonflat, incomplete or contradictory activity
withholds aggregate net economics. Model-vs-venue round-trip differences are restricted
to equal observed quantities and a locally complete modeled report; they are diagnostic,
not evidence that either counterfactual route could have been executed. The report has
no training, promotion, live fill or resume authority.

    python -m scorpion paper-round-trip-reconcile \
      --journal /private/session.jsonl --trust /private/trust.json --trust-sha256 TRUST_PIN \
      --export /private/entries.json --source-artifact /private/entry-source.json \
      --collector-signature /private/entry-collector.sig \
      --reviewer-signature /private/entry-reviewer.sig --as-of CUTOFF \
      --round-trip-export /private/round-trip.json \
      --round-trip-artifact /private/account-activity.json \
      --round-trip-collector-signature /private/round-trip-collector.sig \
      --round-trip-reviewer-signature /private/round-trip-reviewer.sig

A missing local session closure or account reconciliation failure is not a trading loss
or zero return. This offline comparison is not a new execution transport. A supported
authenticated equity venue adapter and independent activity collection are still required.


### Fixed-tape execution stress frontier

The v2 equity replay retains the frozen strategy and explicit modeled-only authority.
Order matching now indexes active orders by symbol in admission order, dropping filled
and deadline-expired orders instead of scanning every historical call on every quote.
Reports expose actual candidate-order visits and the old algorithm's equivalent global
call-scan count; these are operation counts, not a measured latency speedup.
The default scenario uses zero additional delay and 100% displayed depth.

    python -m scorpion paper-market-stress --journal /private/session.jsonl

Read one verified capture and evaluate 12 fixed combinations: additional arrival delay
0/50/250/1000 ms and displayed-depth participation 100/50/25%. No limit, original parent
quantity, decision record or order deadline is retuned. The haircut is applied before
depth admission; unchanged size does not replenish consumed liquidity. Delayed exits can
remain unresolved. The report preserves all scenario denominators, counts quantity changes,
and compares net deltas only for equal positive flat quantities on locally unblocked
model runs. Different quantities remain distinct outcomes, not a selection-adjusted gain.
Neither a grid winner nor any modeled cost difference is a promotion candidate. Filtered
single-venue quote continuity, queue priority and actual fill probability remain unproven.


## P0 external equity paper dispatch pilot

The existing run-session remains intent-only. A separate, explicitly authorized operator
command consumes one fresh, fsync-confirmed prefix intent. It does not connect the agent,
scanner, optimizer or trading model directly to an order transport. The adapter uses
only https://paper-api.alpaca.markets, documented GET account/orders/positions/clock,
POST orders, GET by client_order_id and DELETE order-id endpoints. It does not use live
credentials, configurable URLs, redirects or proxy inheritance. Use a dedicated exclusive
paper account; account ID must match, currency USD, account active. No real orders.

### Preregistered local risk policy

Pin the exact bytes of a private JSON file (sha256sum); the ledger additionally binds the
canonical typed policy. Example (replace account UUID, date and symbols with real,
operator-approved values; amounts are illustrative, not recommended allocations):

    {
      "schema": "scorpion.paper-risk.v1",
      "authority": "PAPER_ONLY",
      "account_id": "00000000-0000-0000-0000-000000000001",
      "session_date": "2026-11-27",
      "maximum_order_usd": "100",
      "maximum_session_buy_usd": "300",
      "maximum_symbol_buy_usd": "150",
      "maximum_group_buy_usd": "200",
      "fee_cash_buffer_usd": "10",
      "maximum_working_orders": 3,
      "symbols_and_groups": {"XYZ": "group-a", "ABC": "group-a"}
    }

The group mapping is an operator-defined exposure grouping, not estimated covariance.
The conservative reservation is cumulative session buy cost plus outstanding limit
commitments; filled purchases consume budget even after sales. Only terminal
unfilled quantity releases its reservation. No leverage, shorts or proceeds recycling.
Current cash/buying power and remaining buy commitments are checked conservatively,
plus the fixed fee buffer. Sell reservations are constrained by confirmed call inventory
and aggregate symbol holdings less pending sells. This is cost-basis commitment, not
mark-to-market, liquidity-at-risk or an account-wide multi-asset risk model.

### Lifecycle and commands

Set SCORPION_ALPACA_PAPER_KEY and SCORPION_ALPACA_PAPER_SECRET locally. Never paste them
into chat, commit them, or put them on command lines. All five commands accept
--ledger /private/paper-orders.db --policy /private/risk.json --policy-sha256 POLICY_PIN.
Initialize once, on a regular exchange date, only from a stable flat account with no
orders. Reconcile and retain each returned ledger chain hash independently outside
the dispatch process. A local hash chain cannot detect a full rollback or clone; keep
one ledger per account/session. Do not reset or fork it if a request is uncertain.

    python -m scorpion paper-order-init [COMMON] --authorize-paper-api
    python -m scorpion paper-order-status [COMMON]
    python -m scorpion paper-order-recover [COMMON] --ledger-head HEAD --authorize-paper-api
    python -m scorpion paper-order-submit [COMMON] --ledger-head HEAD \
      --journal /private/session.jsonl --intent-seq SEQ \
      --authorize-paper-api --authorize-paper-order
    python -m scorpion paper-order-cancel [COMMON] --ledger-head HEAD \
      --client-id CLIENT_ID --authorize-paper-api --authorize-paper-cancel

[COMMON] means the three options above, not literal shell syntax. A conditional EXIT
additionally requires --exit-limit with an explicitly operator-reviewed limit; quantity
is exact confirmed entry inventory (and bounded by the conditional journal quantity).
The source prefix must still be open, have an eligible same-day frozen intent,
no recorded gaps/latch and fresh entry quote. Submit checks the existing safety latch,
venue clock, regular session and exact schedule. The action is a single DAY limit,
whole-share order, no extended hours, replacement, fractional, bracket or option orders.
A later quote must not silently change the recorded entry limit.

DISPATCH is committed with FULL synchronous SQLite before a single POST. Deterministic
client ID derives from account/date/call/role, not a moving prefix hash. Lost ACK, timeout,
HTTP error or crash after the durable transition -> DISPATCH_UNCERTAIN, not retryable.
Only read-side observations advance the order. A 404 is not proof of non-submission.
Cancel is a durable single request; HTTP 204 is not proof of a terminal cancel.
Expired working orders are reported, not automatically canceled; cancel explicitly and
recover. Snapshot comparison double-reads the normalized account, orders and positions,
including all open orders to detect prior-day working orders, and refuses capped results.
Recovery can inspect the account after the original session date; new orders cannot.
It is not atomic venue state; account must remain exclusive. Cash residual within
the pinned fee buffer is not verified fee accounting, and reported average price is
provisional, not individual signed fills. Stage 1/round-trip admission remains independent.

Status without a fresh account snapshot intentionally reports a reconciliation blocker.
A submit or cancel may return nonzero after the network action: never interpret the
exit code as permission to retry. Always inspect the persisted client ID, then recover.
After crash between commit and response, inspect local status and independently reconcile
the head transition before updating the external pin. Never blindly accept a replaced DB.
This is a manually operated paper pilot; automatic deadline cancels, long-running stream
supervision, multi-exit liquidation/replacements, host-level exclusivity, network fault
drills and authenticated vendor verification remain required before unattended paper use.

### Capture benchmark

    python -m scorpion paper-capture-benchmark --samples 2048 --batch 64
    python -m scorpion paper-capture-benchmark --samples 512 --batch 64 --rate 1000

The benchmark drives the real strict journal, fsync and checkpoint callbacks on an
isolated temporary filesystem, verifies every admitted sequence and the final receipt,
and reports producer admission and durability p50/p95/p99/max, actual acknowledged
records/s, bytes/s, workload digest, platform and failures. CI runs both fixed synthetic
workloads. This is not a sustained production feed, venue latency, or 10x throughput claim.
Benchmark the actual deployment volume and failure behavior before increasing limits.


### Deadline cancellation and process-exit verification

A one-shot explicit deadline sweep can be run after an operator has verified the last
head. It reconciles account state, cancels only known overdue working order IDs, persists
each cancel claim before DELETE, then reconciles again. It never submits a new order,
never retries a prior cancel, and leaves uncertain residual exposure reserved.

    python -m scorpion paper-order-sweep [COMMON] --ledger-head HEAD \
      --authorize-paper-api --authorize-paper-cancel

Use one supervised operator process; no scheduled job is installed by this change.
The open-order endpoint roster must match the nonterminal population of the session
orders endpoint, and no prior-session order may be imported into a fresh pilot.
The entry dispatch validity deadline accounts for the initial quote's age and is
rechecked at the transport boundary; the original order expiration remains separate.
The durable dispatch event includes its normalized account snapshot and source digest.
Risk arithmetic runs in a fixed high-precision decimal context. A subprocess regression
exits abruptly immediately after the committed dispatch and proves the reopened ledger
does not release the reservation or allow duplicate submission. This is a process-exit
drill, not proof of power-loss durability on all filesystems.

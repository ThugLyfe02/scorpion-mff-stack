# Scorpion v0.8 — Intelligence Throughput, Causal Redundancy, and Recovery Acceleration

v0.8 is a composition layer on top of the deterministic / precision / calibration / adversarial /
resilience / execution-forensics stack established by PRs #1–#5.

It does **not** change Ryan's locked strategy semantics and it does not add unattended live
securities/options submission.

The objective is to improve a different set of failure modes that emerge as the system becomes
more capable:

- raw Discord delivery must not wait behind expensive stateful processing before becoming durable;
- restart time must not grow linearly forever with the entire normalized event history;
- a single market-data provider must not be able to silently dictate a prepared instruction;
- a second causal clock should validate Discord's API timestamp;
- continuously monitored model quality must not rely on fixed-sample intervals that can be gamed
  by repeated peeking;
- human-review and model-inference budgets should be spent on marginal information rather than
  duplicate obvious cases.

## 1. Durable ingress before stateful work

The Discord callback now crosses the durability boundary first:

```text
Discord Gateway callback
        ↓
FULL-sync raw revision journal
        ↓
bounded asyncio ingress queue
        ↓
single deterministic stateful consumer
        ↓
parse / associate / reduce / atomic normalized commit
```

The queue capacity defaults to 512 and can be configured with:

```bash
SCORPION_INGRESS_QUEUE_MAX=512
```

A bounded queue is intentional. Infinite buffering turns overload into memory growth and delayed
failure. With this design:

1. raw evidence is durable before backpressure occurs;
2. stateful processing remains single-consumer and deterministic;
3. queue pressure is visible through heartbeats and raw-processing backlog;
4. if shutdown occurs before the queue drains, pending raw revisions are recovered by startup;
5. a live normalized-transition failure forces the runtime halt flag immediately so later events
   cannot continue under a stale `NORMAL` resilience assessment.

This eliminates the worst form of head-of-line coupling: a slow normalized transition no longer
prevents the next Discord event from crossing the durable receipt boundary.

## 2. Discord snowflake as an independent causal clock

Discord message IDs are snowflakes. Their high bits encode message creation time.

`discord_snowflake.py` decodes that timestamp locally and compares it against Discord's
`created_at` API timestamp.

This creates two independent causal observations without another network request:

```text
Discord API created_at ─────┐
                            ├─ temporal consistency check
Discord snowflake timestamp ┘
```

A material disagreement becomes `discord_snowflake_clock_mismatch` and fails closed through the
temporal-integrity surface.

Synthetic/non-numeric IDs used by tests/research fixtures remain supported and simply do not
receive snowflake validation.

## 3. Verified policy-bound state checkpoints

Full deterministic replay is the ultimate source of truth, but replay cost should not grow
without bound as years of normalized events accumulate.

`state_checkpoint.py` adds optional operator-created checkpoints.

```bash
scorpion-snapshot --db scorpion.db
```

A checkpoint contains:

- current runtime-policy fingerprint;
- normalized signal count;
- last normalized event ID;
- integrity-ledger record hash for that event;
- serialized deterministic `BookState`;
- independent state fingerprint;
- creation timestamp.

Startup may use a checkpoint **only** when all of the following still match:

```text
policy fingerprint
AND checkpoint signal count <= durable signal count
AND event at checkpoint boundary == stored last_event_id
AND integrity-ledger hash == stored boundary hash
AND decoded state passes invariants
AND recomputed state fingerprint == stored fingerprint
```

Otherwise the checkpoint is ignored and Scorpion performs full replay.

When valid:

```text
verified checkpoint state
        +
normalized events after checkpoint
        ↓
deterministic tail replay
```

Only tail effects need rematerialization because the checkpoint boundary is cryptographically
bound to already-atomic historical transitions.

`scorpion-certify` compares checkpoint+tail state against full replay under the **same explicit
runtime policy**, so faster recovery is continuously proven equivalent to the canonical path.

## 4. Multi-provider live quote consensus

The original code-only fastpath can consume the original single-feed `QuoteCache`.

v0.8 generalizes that dependency to the minimal `QuoteSource` protocol and adds
`ConsensusQuoteCache`.

For each contract, the consensus cache:

1. retains the latest quote independently per provider;
2. removes stale or future-dated observations;
3. requires a configurable minimum provider count;
4. computes provider midpoints using exact `Decimal` arithmetic;
5. measures maximum relative midpoint dispersion around the median;
6. rejects provider sets whose disagreement exceeds tolerance;
7. uses robust median bid/ask aggregation only when the feed set agrees;
8. rejects a crossed aggregate market.

Default posture:

```text
minimum fresh providers = 2
maximum midpoint dispersion = 3%
```

When the consensus cannot be established, `get()` returns `None` and the existing fastpath becomes
`QUOTE_UNAVAILABLE` rather than selecting an arbitrary feed.

This adds market-data redundancy without changing any trading rule or broker boundary.

## 5. Anytime-valid model-quality monitoring

Fixed-sample confidence intervals are not designed for a workflow that asks after every new label:

> "Is the candidate good enough *now*?"

Stopping the first time a fixed-sample interval clears a threshold creates optional-stopping /
repeated-peeking bias.

`confidence_sequence.py` adds a conservative anytime-valid Bernoulli confidence sequence using a
summable allocation of the error probability across all sample sizes.

The bound can be inspected after every observation while retaining the intended global error
budget.

`PromotionEvidence` can now include:

```text
anytime_accuracy_lower_bound
required_anytime_accuracy_lower_bound
```

When supplied, a candidate remains research-only if the anytime-valid lower bound is below the
required threshold.

This supplements Wilson/Brier calibration; it does not replace them. They answer different
questions:

- Wilson/Brier: how accurate/calibrated was this rule on a fixed evidence set?
- confidence sequence: has continuously observed quality accumulated enough evidence without
  exploiting repeated threshold checks?

## 6. Diversity-aware human review allocation

Independent uncertainty sorting can waste scarce review capacity:

```text
10 nearly identical examples from the same rule/contract/channel
```

may crowd out a slightly less uncertain example that teaches the system something new.

`review_allocator.py` keeps the existing active-learning score and greedily optimizes marginal
information gain.

It adds:

- bounded age bonus so old unresolved evidence does not starve forever;
- duplicate-rule penalty;
- duplicate-contract penalty;
- duplicate-channel penalty.

The algorithm remains deterministic and transparent. It is not an ML black box deciding what the
operator may see; it is a budget allocator over already-reviewable evidence.

## 7. Evidence-aware shadow inference routing

Not every Discord message deserves an expensive model call.

`inference_router.py` explicitly separates **money-path authority** from **research compute
allocation**.

Inputs include:

- deterministic parser confidence;
- association confidence;
- novelty score;
- ensemble entropy;
- calibration status;
- parser-conflict evidence;
- source-behavior shift;
- operational mode;
- deterministic event kind.

Routes are:

```text
NONE  — deterministic evidence is already sufficient; spend no model budget
LIGHT — run cheaper shadow/classifier assistance
DEEP  — allocate expensive diagnostic/shadow inference
```

The router assigns cost units and information priority. `allocate_inference_budget()` then spends a
fixed compute budget on the highest-information events first.

Examples:

```text
99% parser + 99% association + trusted rule + low novelty
→ NONE / 0 units

AMBIGUOUS + parser conflict + degraded calibration + high novelty
→ DEEP / 5 units
```

The result cannot mutate deterministic state or create an execution effect. It only determines
where observational/model compute is worth spending.

## 8. Recovery and certification use explicit policy semantics

Two hidden policy seams were removed in v0.8:

1. runtime certification previously called replay through implicit defaults;
2. backup verification previously reconstructed source/backup state through implicit defaults.

Both now use an explicit `RuntimePolicyBundle`.

A recovery snapshot includes the policy fingerprint, and backup equivalence verifies that source
and backup were reconstructed under identical semantics.

`scorpion-certify` additionally proves:

```text
full replay fingerprint == verified checkpoint + tail fingerprint
```

under the current runtime policy.

## 9. Extended schema contract

Schema contract v2 recognizes advanced surfaces introduced by the composed stack:

- execution delivery ledger;
- component release registry;
- state checkpoints;
- decision packets;
- integrity ledger;
- stage timing.

Advanced surfaces remain optional when unused, but if a table is present with missing required
columns the database is declared incompatible rather than partially trusted.

## 10. Operational guarantees

The v0.8 runtime hierarchy is now:

```text
Discord message
  ↓
independent API/snowflake clock evidence
  ↓
durable raw journal
  ↓
bounded ingress backpressure
  ↓
single deterministic processing consumer
  ↓
parser / association / sequence / strategy / resilience
  ↓
policy-bound decision packet
  ↓
atomic normalized transition
  ↓
optional consensus market-data source
  ↓
code-only prepared instruction
  ↓
durable exactly-once delivery identity
  ↓
paper or explicit human-authorized integration boundary
```

Recovery:

```text
verified policy-bound checkpoint available?
   ├─ yes → restore + replay tail + certify equivalence
   └─ no  → full deterministic replay
```

Intelligence budget:

```text
trusted deterministic evidence
→ no expensive model call

moderate uncertainty
→ light shadow inference

novel / conflicting / ambiguous / degraded evidence
→ deep shadow inference if compute budget permits
```

## Authority boundary

v0.8 adds autonomous durability, backpressure, validation, compute allocation, checkpoint fallback,
market-data disagreement rejection, and statistical quality monitoring.

It does **not** autonomously:

- submit live securities/options orders;
- activate a component release;
- promote a parser/model/strategy;
- promote ETF/lotto research buckets into execution eligibility;
- choose personalized live position sizing.

The strongest autonomous behavior remains increased scrutiny, quarantine, withholding, fallback,
or routing work to operator review.

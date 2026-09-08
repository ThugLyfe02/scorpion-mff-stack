# Precision Intelligence Layer

This layer is intentionally stacked **on top of** the deterministic control-plane PR. It does not change `live/RULES.lock.md`, the MFF source channels, the research-only scanner boundary, or Ryan's sizing/strategy semantics.

Its purpose is narrower and more valuable: make interpretation quality, recovery correctness, and latency measurable enough that failures become visible before they become account-level mistakes.

## High-impact gaps sealed

### 1. Parser self-awareness
Every parse now produces structured evidence alongside the normalized event:

- rule ID
- calibrated confidence band
- matched terms
- conflicting action families
- normalized text
- parser latency in microseconds

The money path is still deterministic. Confidence is telemetry, not permission to weaken a rule.

### 2. Golden-corpus regression gate
`tests/fixtures/mff_golden.json` is a versioned set of representative MFF-style entry/add/trim/exit/ambiguous/ignore phrases. CI fails if a parser change changes expected semantics.

The corpus is deliberately small and explicit today. It should grow from human-reviewed raw messages, never from lossy `trades.json` summaries.

### 3. Contract association evidence ladder
Follow-ups are associated only when evidence yields exactly one target:

1. Discord reply/reference
2. exact contract already present
3. explicit ticker (+ side when present)
4. unique same-author + same-channel source lineage
5. unique live position
6. otherwise unassociated → review

Every association records method, confidence, and candidate count.

### 4. Revision-aware Discord edits
Discord message edits are first-class immutable raw revisions. Event IDs include normalized content + edit revision, so an edited premium/expiry cannot collide with the original event ID.

An edited entry that conflicts with an already-open generation is therefore surfaced through existing stale/duplicate review logic rather than silently overwriting state.

### 5. Crash-boundary recovery
Raw capture and normalized signal creation are separated by a real failure boundary. `raw_processing` now journals every captured revision as `PENDING` until the deterministic pipeline completes it.

On startup, any pending raw revision is replayed through the normal parser/association/reducer path. Recovery failure halts startup instead of silently skipping the message.

### 6. State invariants + replay fingerprints
Every state transition is validated for impossible conditions such as negative quantity, closed positions with quantity, missing source lineage, or max-open violations.

`state_fingerprint()` creates a stable SHA-256 digest of deterministic state. A restarted/replayed process can prove it reconstructed the exact same book state.

### 7. Quality + latency health, not just process health
`decision_audit` records parser and end-to-end pipeline latency plus confidence/association metadata.

Rolling health exposes:

- ambiguity rate
- low-confidence rate
- unresolved-association rate
- parser p95 latency
- pipeline p95 latency
- pending raw revisions

This catches a system that is technically online but semantically degrading.

### 8. Human adjudication feedback loop
Reviewed events can be labeled with the expected event kind and contract. `scorpion-accuracy` calculates observed accuracy, actionable precision, ambiguity rate, and per-class precision/recall/F1 from adjudicated events.

This gives parser changes an empirical promotion criterion instead of intuition.

### 9. Shadow-model harness
A future LLM/classifier may run only as a side-channel `ShadowClassifier`.

Its output:

- never changes deterministic state
- never creates effects
- is stored with model/version/confidence/latency
- is compared against deterministic output and later human adjudication

This allows aggressive experimentation with richer models while preserving a hard execution boundary.

### 10. Drift detection
The evaluation utilities compare event-kind distributions using total variation distance. Sudden shifts in `ENTRY/EXIT/AMBIGUOUS/IGNORE` rates can flag source-language drift, parser regressions, or upstream behavior changes.

### 11. Candidate promotion gates
Candidate parser/model versions can be diffed against the baseline before promotion. The comparison explicitly counts action escalations where a candidate turns `IGNORE/AMBIGUOUS` into an actionable event, plus contract reassignment.

Promotion criteria can require minimum adjudicated sample size, accuracy, actionable precision, ambiguity rate, latency budgets, and zero unexplained action escalations.

## Promotion discipline

A parser/model version should not be trusted more simply because it covers more messages. For the money path, the preferred ordering is:

1. minimize false actionable classifications;
2. maximize actionable precision;
3. then improve recall/coverage;
4. keep ambiguity explicit where evidence is insufficient;
5. verify latency remains within budget;
6. shadow-run against real raw messages before operational reliance.

The objective is not maximum automation. The objective is maximum **correct automation per unit of irreversible risk**.

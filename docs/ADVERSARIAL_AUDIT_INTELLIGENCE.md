# Adversarial Audit Intelligence (v0.4)

This layer is stacked on top of the green deterministic, precision, and calibrated-semantic layers. It does not change Ryan's locked MFF strategy semantics and does not add autonomous live brokerage submission.

## Fail-closed action context

Parser v3 adds explicit pre-entry guards for negated and historical action language. Examples such as:

- `Do not buy QQQ 719C TODAY @ 1.01`
- `Not closing runners yet 19%`
- `Closed QQQ earlier at 19%`

route to `AMBIGUOUS` instead of being interpreted as a current actionable instruction. The guard runs before contract-entry recognition so a valid-looking option specification cannot override an explicit negation.

## Adversarial surface mutation

`adversarial.py` generates deterministic presentation mutations including whitespace variation, line breaks, case changes, zero-width characters, and punctuation spacing. The robustness evaluator compares event kind and contract identity against the baseline parse.

This makes parser robustness measurable instead of relying on a handful of hand-written examples.

## Stateful raw counterfactual replay

`counterfactual.py` replays raw Discord revisions through a candidate parser, follow-up association, reducer, invariants, and source-message lineage. It produces a deterministic state fingerprint and effect stream.

This is stronger than label-only parser comparison because it reveals whether a candidate changes downstream state, reviews, or proposed effects after realistic message ordering and follow-up association.

## Persisted evidence bridge

`evidence_bridge.py` connects the v0.3 calibration/ensemble primitives directly to persisted production evidence:

- human adjudications + decision audit → per-rule calibration;
- human adjudications + shadow predictions → per-model reliability;
- reliability → conservative ensemble weights using Wilson lower bounds once sample support is sufficient.

This closes the gap between offline metric utilities and the actual audit database.

## Tamper-evident integrity ledger

`integrity.py` provides an append-only SHA-256 hash chain. Each newly accepted normalized transition is chained inside the same atomic transaction that persists the signal, effects, decision audit, raw completion marker, and heartbeat.

The chain can detect missing, reordered, or modified ledger records under ordinary partial/accidental tampering.

### Threat-model limitation

A fully privileged attacker who can rewrite the entire database can also recompute the complete chain. For that threat model, periodically anchor `IntegrityLedger.head_hash()` in an independent system. External anchoring is intentionally outside this package rather than pretending an in-database hash chain solves that problem by itself.

## Why this is competitive

The stack now supports a disciplined parser/model development loop:

1. capture raw immutable evidence;
2. replay candidates counterfactually;
3. fuzz presentation surfaces;
4. measure downstream semantic changes;
5. calibrate rule/model confidence from human adjudication;
6. prioritize high-information review;
7. verify evidence lineage;
8. promote only versions that pass accuracy, semantic, latency, and safety gates.

The goal is not maximum automation. It is maximum measurable correctness per unit latency while retaining a deterministic, reviewable money-path boundary.

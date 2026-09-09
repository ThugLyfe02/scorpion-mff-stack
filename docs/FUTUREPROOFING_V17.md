# Scorpion v0.17 — conditional economic safety + evidence hardening

v0.17 tightens the distinction between a statistically interesting challenger and an economically safe challenger. The layer remains research/governance-only: it can reject evidence, quarantine degraded components, and prepare operator review, but it cannot activate a model or submit a live order.

## 1. Conditional policy safety

Aggregate paired reward can hide concentrated harm. A challenger that improves ordinary days while materially degrading a stressed regime is not a safe improvement merely because the overall mean stays positive.

`conditional_policy_safety.py` evaluates caller-supplied, pre-declared slices such as:

- volatility or market regime
- liquidity tier
- source channel
- contract family
- time-of-day bucket

The gate requires sufficient global evidence and sufficient powered-slice coverage, then measures:

- global mean daily paired improvement
- lower-tail mean daily improvement
- per-slice mean paired improvement
- per-slice positive-day ratio
- block-bootstrap lower bounds
- a Bonferroni-style familywise adjustment across powered slices

The evaluator does not search for a flattering slice after seeing outcomes. Slice construction belongs to the research manifest so the statistical question is declared before the promotion decision.

## 2. Cost- and turnover-adjusted OOS value

Better probabilities or gross reward are not enough if the improvement disappears after spread, slippage, queue effects, fees, or increased trading activity.

`cost_adjusted_policy_value.py` accepts paired held-out/shadow outcomes with explicit:

- gross reward
- execution cost
- net reward
- turnover
- chronological fold identity

The gate evaluates mean net improvement, a block-bootstrap lower bound, positive-day and positive-fold ratios, worst-fold net delta, net capture of gross improvement, and challenger/incumbent turnover ratio.

This is intentionally downstream of causal OOF/shadow evidence. It does not invent a live execution model or position size; it asks whether supplied execution-aware evidence still supports the challenger.

## 3. Governance composition

`PromotionEvidence` now accepts:

- `conditional_policy_safety`
- `cost_adjusted_policy_value`
- `require_economic_safety_gates`

Existing callers remain compatible. New v0.17 challenger workflows can set `require_economic_safety_gates=True`, in which case both reports must be present and qualified before the candidate can become `READY_FOR_OPERATOR_REVIEW`.

Passing still means only that automated evidence is mature enough for a human operator to review. It is not deployment authority.

## 4. Evidence and recovery hardening

The same change set closes several evidence-integrity gaps found during PR review:

- runtime certification rejects missing database paths before SQLite can create a new empty database;
- certification fails when normalized signals exist outside the integrity ledger;
- backup verification rejects missing source or backup paths instead of creating empty schemas;
- recovery treats uncovered signals as failed source-bound evidence;
- checkpoint restore re-verifies current database evidence before trusting a serialized prefix;
- causal trace binds to the exact raw revision recorded in integrity evidence rather than the newest edit for a Discord message;
- component release identity binds the predecessor release, preventing ambiguous rollback lineage;
- the live ingress consumer stops after a failed state transition so later queued revisions cannot advance normalized state ahead of the unresolved failure;
- every configured execution-uncertainty scenario counts toward robustness, including harsh scenarios that become sample-insufficient.

## 5. Research interpretation

The two additional questions v0.17 forces are deliberately simple:

1. **Where does the challenger lose?** A positive headline mean is not enough if a powered regime, liquidity state, or tail slice is harmed.
2. **Does the improvement survive economic friction?** Predictive lift is not trading edge if realistic execution costs or turnover consume it.

These gates complement, rather than replace, the existing purged walk-forward, PBO/selection adjustment, temporal cross-fitting, heavy-tail robustness, feature-family selection, nested candidate selection, conformal calibration, regime stability, capacity analysis, and targeted regression firewall.

## 6. Authority boundary

v0.17 may autonomously evaluate evidence, reject or quarantine a challenger, run research, and prepare operator-review artifacts. It does not:

- submit unattended live securities/options orders;
- choose personalized live position sizing;
- promote a parser/model/strategy into production;
- activate a rollback release;
- change locked MFF strategy semantics or execution eligibility.

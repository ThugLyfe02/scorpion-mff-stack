# FUTUREPROOFING v0.32 — attested causal meta-learning

v0.32 tightens the research process itself as a causal system. The goal is not another model; it is to make adaptive research decisions increasingly evidence-efficient without allowing Scorpion to manufacture confidence from its own selection process.

## 1. Internally attested cluster randomization

`research_randomization_attestation.py` removes caller-selected treatment draws from the strict research path.

A randomization unit is keyed by `(experiment_id, wave_id, cluster_id)` and binds:

- the exact treatment distribution hash,
- 256 bits of OS-generated entropy,
- an entropy commitment,
- the derived random draw,
- cluster/wave/experiment identity,
- immutable record/event hashes.

Assignments sharing one cluster/wave reuse the exact same randomization unit. This turns cluster-level interference from an implicit assumption into an explicit design choice and prevents related observations in the same wave from receiving mutually contaminating treatments.

The strict causal evaluator requires assignment attestations. Direct legacy assignments remain valid for v0.31 compatibility, but they cannot enter the v0.32 evidence path.

## 2. Certified cross-fit provenance

`crossfit_provenance.py` makes held-out prediction identity explicit.

Each manifest binds:

- model fingerprint,
- dataset fingerprint,
- accepted-truth snapshot,
- deterministic split-plan hash,
- assignment universe hash,
- fold-specific held-out assignment IDs,
- training-assignment-set hash,
- fold-specific training truth snapshot.

A prediction is accepted only if its assignment is explicitly in the held-out fold named by the prediction and the manifest/model hashes match.

This does not merely label a prediction `crossfit`; it makes the exclusion claim mechanically checkable.

## 3. Treatment-specific censoring + cluster-robust AIPW

`counterfactual_learning_efficiency_v2.py` closes the global-resolution-rate blind spot.

Before estimating treatment value it checks:

- experiment ledger integrity,
- randomization-attestation integrity,
- full attestation coverage,
- treatment-specific matured/resolved counts,
- minimum per-treatment resolution rate,
- maximum between-treatment resolution-rate gap,
- cross-fitted outcome-model provenance,
- cross-fitted censoring-model provenance,
- minimum censoring probabilities,
- treatment support.

Treatment value uses an augmented inverse-probability estimator with joint behavior/censoring correction:

`mu_a(X) + I(A=a,R=1)/(p(A=a|X) q(R=1|A,X)) * (Y - mu_a(X))`

Joint inverse weights are capped. Effective sample size is enforced. Old evidence is time-decayed. Current-regime estimates are shrunk toward global evidence. Uncertainty is computed over interference clusters rather than pretending correlated events are independent. Simultaneous lower bounds are applied across supported treatments.

## 4. Causal allocator calibration v2

`causal_allocator_calibration_v2.py` adapts qualified v0.32 causal evidence to the existing information-value allocator protocol.

The evidence hierarchy is now:

1. hand-designed information value,
2. realized observational learning yield,
3. v0.31 doubly robust treatment evidence,
4. v0.32 attested/censoring-aware/cluster-robust causal evidence.

The exact knapsack remains unchanged. Only the evidence used to price research actions becomes stronger.

## 5. Whole-trajectory sequential DR

`sequential_learning_ope.py` evaluates complete multi-step research policies instead of indefinitely composing pairwise synergies.

For each completed research episode it requires:

- all steps matured,
- all outcomes resolved,
- every step attested,
- target-policy support,
- held-out cross-fit Q provenance,
- one interference cluster per episode.

It then uses cumulative target/behavior importance ratios and the sequential DR correction:

`Vhat(s1) + sum_t rho_1:t * [r_t + gamma Vhat(s_{t+1}) - Qhat(s_t,a_t)]`

Cumulative ratios are capped, episode-weight ESS is enforced, and uncertainty is cluster-robust.

This lets Scorpion compare full information-gathering programs such as:

`DEEP_SHADOW -> HUMAN_REVIEW -> CURRICULUM_INCLUSION`

without assuming pairwise effects compose indefinitely.

## 6. Negative-result and hypothesis-family memory

`research_hypothesis_memory.py` prevents the research loop from repeatedly rediscovering the same attractive failure under slightly different wording.

Each hypothesis has:

- canonical normalized claim,
- claim hash,
- hypothesis family,
- parent/generation lineage,
- mechanism scope,
- intervention scope,
- immutable resolution history.

Resolutions bind evidence IDs, datasets, regimes, time blocks, selection-family identity, and familywise alpha consumed.

A previously falsified/inconclusive claim is not automatically re-run. It may reopen only when:

- a materially new regime is encountered,
- a materially new dataset is encountered,
- or configured temporal staleness justifies revalidation.

Selection-family alpha consumption is cumulative and enforced before a resolution is accepted.

## 7. Durable research insight outbox

`research_insight_outbox.py` makes replicated discoveries durable operator-visible state.

A `BREAKTHROUGH_CANDIDATE` can enter the outbox only after the existing breakthrough replication gate passes. Insights and acknowledgements are append-only and independently hash-chained. Pending insights remain queryable until an explicit acknowledgement event exists.

This is intentionally not model promotion. Discovery visibility and production authority remain separate.

## 8. Authority boundary

v0.32 is research-only.

It may autonomously improve:

- experiment design,
- randomization integrity,
- counterfactual attribution,
- censoring correction,
- research-budget calibration,
- sequence evaluation,
- negative-result memory,
- breakthrough surfacing.

It does not autonomously:

- activate production models,
- modify locked strategy semantics,
- submit securities/options orders,
- activate rollback releases,
- choose live personalized position size.

## 9. Remaining high-value seams

The strongest next gaps are deliberately documented rather than hidden:

1. external/remote entropy anchoring for randomization so local process compromise cannot bias assignment entropy;
2. richer censoring models for informative delayed outcomes and competing-risk resolution states;
3. network/interference graphs when spillover crosses cluster boundaries;
4. sequential censoring correction for partially observed multi-step episodes;
5. model-based experiment design that maximizes expected causal information gain while preserving mandatory exploration floors;
6. durable hypothesis graph search that proposes novel child hypotheses while debiting the correct statistical family budget;
7. research-resource shadow prices so reviewer time, GPU time, annotation latency, and opportunity cost share one constrained optimization surface.

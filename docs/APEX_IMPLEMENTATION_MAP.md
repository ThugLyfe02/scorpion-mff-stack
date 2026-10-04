# APEX implementation map

## Purpose and status

APEX extends the existing Scorpion research and control systems. The implementation unit is a small, falsifiable change with a reviewable diff. Adding an algorithm is not a milestone; demonstrating that it corrects a known failure or creates validated marginal value is.

This map distinguishes four states:

| State | Meaning |
|---|---|
| **Existing** | An implementation and relevant tests were located. This does not establish statistical validity or prospective value. |
| **Correctness gap** | The implementation has a specifically identified limitation or reproduced counterexample. |
| **Implementation gap** | The capability was not located in the audited source. Search again before creating a file. |
| **Prospectively unvalidated** | A claim of future market, execution, calibration, or research-allocation improvement has not been established by the evidence reviewed for this map. |

The detailed MFF capability audit used exact source **`9f17d43f3f70d508278370b33e44b014d3cadfa4`**, the original upper research/scanner branch. Program A has since reconciled and published the lower control hardening through MFF PR3–PR6. The verification snapshot below records each published source head, its full-suite total and CI evaluated at that literal head. These are overlapping suites on different branches; their totals must not be added as independent coverage.

| MFF branch | Published reconciled source SHA | Full-suite tests passed | Literal-head CI |
|---|---|---:|---|
| PR3 | `00c594fb2b9b477c8ae9286d8aa2d4d6f64cfcf5` | 78 | Green |
| PR4 | `8f41fadac3bcac545246119aca08e83fed4a9fa4` | 168 | Green |
| PR5 | `c75afb7b08730e7cc7cd6fba67760d550645e67d` | 482 | Green |
| PR6 | `7942c0ff59689c7d11a78c972c22a65da7c54626` | 536 | Green |

The completed MFF DAG inventory contains 554 reachable commits and seven actual branch heads. The initial inventory contained 545 commits and eight refs, one of which was the `origin/HEAD` alias. Ref counts and distinct branch-head counts are different quantities; the alias must not be presented as an additional branch.

Eight source-stable captures retained as `docs/apex/program-a/captures/{original,reconciled}-pr{3..6}.json.gz` in the MFF evidence pack preserve the original and reconciled evidence. Each original-to-reconciled pair has **zero changed cases across the same 40-case semantic corpus**, with 39 complete cases and one explicitly incomplete February-30 parser case. All four differential reports say `IDENTICAL_WITH_INCOMPLETE_CASES`. Matching an existing failure is not a passing semantic test. Exact branch topology, source and policy fingerprints, CI run identities, connection coverage, review findings and residual risks must be consolidated in [APEX_BASELINE_ATTESTATION.md](APEX_BASELINE_ATTESTATION.md).

Stock Finder findings in this map refer to its audited PR6 research/evidence line. Additional active private Stock Finder research branches were **not exhaustively inspected by this capability audit**. Absence from this map is not proof that a feature is absent from every branch. Recheck current branch ancestry, repository contents and parallel research work before adding anything.

**Current gate: BLOCKED.** MFF and the scanner producer/consumer contract checks pass, but exact-head full-suite CI on the separately active Stock Finder research lineage exposed reproducibility gaps. The baseline attestation records successful checks and failed prerequisites separately. No new APEX mathematics begins before that gate closes. No APEX mathematics has been implemented as part of this map; B–J below are proposed increments, not completed milestones. The first Program B change after the gate is **B0: repair the existing PBO diagnostic**.

## Rules applying to every increment

- **Authority impact: NONE.** Stock Finder remains `RESEARCH_ONLY`. Models, optimizers, bandits, experiment allocators and candidate generators receive no preparation, approval or dispatch capability. Output types describe research evidence, shadow evaluation or diagnostic context.
- Preserve `live/RULES.lock.md`, source interpretation and execution fidelity. An alternative hypothesis receives a separate research identity. No automatic contract substitution, live order, sizing change or self-promotion is included here.
- Record the current failure and its measured frequency. If frequency is not known, record `UNKNOWN`; do not invent prevalence to justify complexity.
- Identify whether the failure can be recognized before the decision outcome and state the cheapest credible counter-test. The current frequency of most proposed research failures is `UNKNOWN`; the existing code inventory alone does not establish their prevalence.
- Preregister endpoint, baseline, minimum practical effect, protected metrics, sample requirement, stopping rule, missing-data policy, family and data cutoffs before access to evaluation outcomes.
- Name the new failure mode introduced by the component: numerical instability, temporal leakage, unsupported causal identification, extra dependencies or operational coupling as applicable. The PR must explain how its invariant and counterexamples constrain that risk.
- Report LOC, dependency, configuration, TCB and latency deltas. Heavy research stays outside the critical process. A runtime/ingress/state/quote/preparation change requires comparable before/after p50, p95 and p99 at minimum, plus the complete available tail distribution.
- Evaluate each new model against the unchanged incumbent, a simple heuristic/statistical baseline and an appropriate null or negative control. Use matched samples and include coverage, expectancy, tail behavior and execution feasibility where relevant.
- Give insufficient, invalid, unidentified or unstable evidence explicit states. A missing estimate is not zero; a hash is not proof that its input was true or available in time.
- Preserve historical reports and their metric/code versions. Corrections create new evidence identities; they do not silently overwrite an earlier conclusion.
- Every optional subsystem needs a removal path. Insufficient prospective marginal value produces a deprecation recommendation and ordinary human review. Research automation cannot remove a safety invariant or grant itself authority.

The file lists below are extension targets, not authorization to modify all of them in one PR. Each PR should touch only the surfaces needed for its increment.

**Path convention:** Unqualified MFF module filenames below mean `src/scorpion/<filename>` in `scorpion-mff-stack`; test filenames mean `tests/<filename>`. Exact private Stock Finder extension paths remain in its companion map. Documentation and workflow paths are given relative to their repository root.

## Program A — Establish and attest the reconciled baseline

### A0 — Lineage and exact-source evidence

**Existing modules reused:** `policy_bundle.py`, `provenance.py`, `semantic_replay.py`, `replay.py`, `hot_path_benchmark.py`, existing CI workflows and golden fixtures. Reuse the original PR3/PR4/PR5/PR6 source history rather than rebuilding the stack.

**Completed MFF evidence:** The four published source heads above contain the deliberate control reconciliation, have passing full suites and literal-head CI, and have matching original-to-reconciled semantic captures. Benchmark and source manifests are saved with those captures. **Still required to clear the cross-repository gate:** Correctly identified evidence inputs and successful full-suite CI for the separately active Stock Finder research lineage. Its baseline attestation preserves each failed run and its disposition.

**Files to modify:** Complete `docs/APEX_BASELINE_ATTESTATION.md` and retain the exact `.github/workflows/ci.yml` verification already applied to the MFF source heads; remaining Stock Finder guard changes belong in their owning existing workflows/tests. **New files justified:** `docs/APEX_BASELINE_PROBE.md`, the evidence pack under `docs/apex/program-a/`, and `tools/verify_apex_evidence.py` for verifying that pack. Preserve the exact capture method as `docs/apex/program-a/probes/apex_baseline_probe.py.gz`, whose decompressed SHA-256 is `0a3d5be865f021629054838e43278fc5dad270c70da8f98bd4c1d65ebe105633`. The frozen helper calls existing parser, association, reducer, Store, replay and benchmark APIs. Do not add a second edited helper under `tools/` or introduce a new benchmark implementation. The pack verifier is the only new runnable utility in the baseline documentation PR; the frozen method is retained for cold reproduction.

**Falsifiable question / baseline:** Does each intended descendant contain the required lower-layer control behavior, and can differences from its exact original head be explained? The baseline is the original SHA and immutable shared corpus, not a moving branch name.

**Invariant:** Reconciliation preserves locked strategy semantics and introduces no unexplained action escalation. Changed parser/effect/state behavior must be listed, not hidden by a relaxed comparison.

**Benchmark:** All eight captures used the same helper, golden corpus and interpreter with 320 samples per common path, run sequentially while local agents were paused. The common core path measures parser/association/reducer/invariant compute with the same valid guild and fixed templates; each head produced 107 `ENTRY`, 107 `AMBIGUOUS` and 106 `IGNORE` events. The common durable `Pipeline.handle()` path uses a valid guild/author and a deliberately rejected channel, retaining 320 raw events, 320 normalized events and zero effects per capture. Store/Pipeline startup, network, quotes and live dispatch are excluded.

The original native PR5/PR6 benchmark v1 used an unrecognized guild and all core inputs were ignored. Reconciled native v2 uses the configured guild and verifies substantive event-kind counts. Those native workloads differ; use the common paths for comparisons across reconciliation. Do not present native v1-to-v2 differences as a parser speedup or slowdown.

The common core repeatedly visits one pending contract, an ambiguous follow-up and a rejected symbol; it is not a representative live-state mix. No measured warm-up samples are discarded, but semantics run first in `--mode all`, so the capture is not a cold-start benchmark. The frozen helper is evidence of the recorded method, not a globally lint-certified production component.

| PR6 common workload | Source | Unit | p50 | p90 | p95 | p99 | p99.9 | Max |
|---|---|---|---:|---:|---:|---:|---:|---:|
| Core compute | Original | µs | 38.067 | 68.191 | 107.129 | 211.304 | 241.137 | 241.137 |
| Core compute | Reconciled | µs | 30.465 | 84.185 | 124.926 | 202.371 | 237.012 | 237.012 |
| Durable control path | Original | ms | 1.755 | 2.334 | 2.705 | 5.782 | 14.970 | 14.970 |
| Durable control path | Reconciled | ms | 2.382 | 3.509 | 5.946 | 10.146 | 38.040 | 38.040 |

These measurements preserve an observed increase in durable-path latency; they do not establish its cause or economic cost. Original PR4–PR6 lacked the replay sentinel; original PR3 and all reconciled heads report the actual sentinel present, cadence 64 and commit counter increasing from 0 to 320. Those counters show configured participation, not direct sentinel timings or corruption-response proof. A stronger control path is part of the comparison. At 320 samples p99.9 is the maximum, not a stable production-tail estimate. CPU, Python, OS, SQLite, clock and overlay-filesystem identity are recorded; physical storage media and host-wide competing work remain unknown. Neither common path is an executable-order latency claim. Sources: the eight `benchmark.json` files and `mff-pr{3..6}-semantic-diff.json` originally collected under `apex-evidence/`; their retained pack identities belong in the baseline attestation.

**Validation / deletion rule:** The final cross-repository attestation must cite the completed exact-head local/CI records and resolve the remaining Stock Finder checks before Program B. Preserve the incomplete malformed-expiry finding; green CI cannot silently overrule it. This is software/operational validation, not prospective trading validation. Remove redundant capture code if an existing command can produce the same complete evidence; retain the attestation and fixtures. **Authority: NONE.**

### A1 — Reconcile control semantics, then prove recovery

**Existing modules reused:** `pipeline.py`, `runtime.py`, `store.py`, `transactional.py`, `integrity.py`, `processing_order.py`, `execution_state.py`, `execution_journal.py`, `state_checkpoint.py`, `recovery.py`, `certification.py`, `fault_certification.py`. The execution-state/journal surfaces are inherited from the lower hardening lineage.

**Correctness gaps addressed in the published MFF source:** Stale descendants have inherited halt-before-authority behavior, integrity sentinel behavior, connection-specific SQLite settings, admitted-versus-observed state separation and durable processing order. These repairs are supported by the per-head suites and captures; this map does not replace the final per-invariant attestation or claim that the 40-case parser corpus exercises every runtime path.

**Files to retain / modify only for a demonstrated residual:** The existing control modules and `test_transactional_pipeline.py`, `test_precision_runtime.py`, `test_integrity_sentinel.py`, `test_execution_journal.py`, `test_execution_forensics.py`, `test_runtime_sqlite_connections.py`, `test_invariants.py`, `test_scanner_context_v33.py` and `test_scanner_authority_boundary.py`. **New files:** None required beyond a focused regression test when an existing suite cannot express a failure clearly. Do not reopen reconciled semantics to add research features.

**Falsifiable question / baseline:** Can a halted, blocked, duplicated, failed or out-of-order event create an unauthorized effect, consume admitted-book state, bypass an earlier pending event, or reconstruct the wrong book after restart?

**Invariant:** Observed checkpoints reconstruct the observed book; persisted admission decisions reconstruct the admitted book; confirmed fills reconstruct execution truth. Source-time sorting must not replace durable processing order in operational recovery. A halted process may continue safe durable capture but may not create newly authorized executable effects.

**Benchmark:** Preserve durability while measuring connection/transaction and the specified control-path tail cost. The capture observes one actual `Store.connect()` connection: reconciled heads report WAL, `synchronous=2`, foreign keys enabled and a 5000-ms busy timeout. That observation is not a per-writer proof. The native benchmark reads PRAGMAs on a separate bare SQLite connection. Use the connection audit and `test_runtime_sqlite_connections.py` for the broader runtime requirement.

**Validation / deletion rule:** Retain lower-layer regression suites, halt races, scanner boundaries, replay sentinel faults, fill accounting and checkpoint/restart cases. Convert every discovered counterexample into a permanent regression. Remove temporary compatibility shims when their callers are reconciled; never remove the invariant to improve a benchmark. **Authority: NONE; existing authority can only be constrained by these fixes.**

## Program B — Quantitative reality checks

### B0 — Correct existing PBO before adding statistics

**Existing modules reused:** `backtest_overfit.py`, `statistical_universe.py`, `evidence_lattice.py`; `tests/test_research_intelligence_v12.py` and `tests/test_evidence_lattice_v13.py`.

**Reproduced correctness gaps at the audited SHA:**

| Input / operation | Original behavior |
|---|---|
| Two identical all-zero return series | `PASS`, PBO `0.0` |
| Two positive-infinity return series | `PASS`, PBO `0.0`, OOS mean `inf` |
| Finite winner and loser plus an all-NaN candidate | `PASS`, PBO `0.0` |
| Median candidate among three | Relative rank `2/3`; reference CSCV uses `2/(3+1)` |
| Combination cap | Entire combination list materialized before the cap is applied |

**Files to modify:** `src/scorpion/backtest_overfit.py`, existing PBO tests, narrowly scoped methodology documentation, and callers only if explicit invalid/missing-report semantics require it. **New files:** No production module. A focused test file is justified if it makes the numerical contract easier to review.

**Falsifiable question / baseline:** Can non-finite or uninformative evidence obtain a successful robustness result, and can a configured bounded task allocate an unbounded combination family? The listed failures and the existing stable-winner/slice-specialist fixtures are the baseline.

**Implementation:** Validate finite numeric panels before applicability/sample shortcuts; validate integer policy counts; use reference `rank/(N+1)` normalization; specify treatment of ties and degenerate competition; disclose mean-return selection. Refuse unsupported combinatorial work explicitly, or implement bounded complement-symmetric sampling with separately disclosed coverage. Do not label partial sampling as exhaustive CSCV. Do not infer performance significance from a low PBO alone.

**Invariant / benchmark:** Invalid evidence cannot qualify; rank/logit values match independent reference vectors; the work budget bounds actual work and memory. Benchmark the existing 8-slice/32-period case and oversized-work refusal. No critical-path imports or dependency changes.

**Validation / deletion rule:** Numerical regression, reordering invariance, NaN/Inf including undersized inputs, ties, malformed policy types and integration gates. This increment establishes correctness, not market lift. Retain the corrected existing module; delete an alternative implementation if it duplicates the same role. **Authority: NONE.**

### B1 — Make Sharpe assumptions and track-record requirements explicit

**Existing modules reused:** `selection_adjusted.py`, `hac_edge.py`, `quant_audit.py`, `statistical_universe.py`, `provenance.py`. Existing selection-adjusted code contains a non-normal Sharpe standard error and an IID-null trial hurdle; HAC is a mean-edge diagnostic.

**Missing capability:** An explicit PSR benchmark/unit contract, defensible trial-Sharpe variance and independent-trial sensitivity for DSR, minimum track-record requirements, and clearly distinct serial-dependence diagnostics. The current `1/(n-1)` trial variance assumption is not a substitute for a fully documented DSR.

**Files to modify:** Those existing statistical/report modules and `test_quant_ml_v13.py` / `test_hac_edge_v16.py`. **New files:** None initially.

**Question / baselines:** How much does a reported risk-adjusted claim weaken after non-normality, sample length and documented search breadth? Compare zero-benchmark PSR, declared-benchmark PSR, the current hurdle and declared DSR sensitivity; retain the HAC mean result separately. A daily sum of trade return fractions must not be mislabeled a capital-normalized portfolio-return series.

**Invariant / benchmark:** Matching return units/frequency; finite estimable moments; no silent annualization; no arbitrary substitution of ESS into an IID formula. Use published numerical cases and an independent slow calculation; benchmark off-process by series/trial count.

**Prospective validation / deletion rule:** Historical diagnostics can veto or discount a claim but do not confirm prospective expectancy. Freeze protocol and collect new sessions for that claim. Remove redundant reports and unsupported “DSR” labels if the required trial evidence is absent. **Authority: NONE.**

### B2 — Record search pressure and allocate statistical capital before testing

**Existing modules reused:** MFF `research_hypothesis_memory.py`, `research_experimentation.py`, `provenance.py`, `dataset_fingerprint.py`; the companion Stock Finder alpha, hypothesis, epoch, lockbox and lifecycle registries. Exact private extension paths are recorded in the private companion map.

**Correctness / implementation gap:** MFF records hypothesis ancestry and resolution-time alpha spending. That does not enforce registration-time allocation or capture every feature, threshold, horizon, model, overlapping sample and lockbox contact. Deduplicating strategy return sets must not erase the original research search.

**Files to modify:** Existing hypothesis/manifest/epoch registries and their tests in the owning repository. **New files:** None until current private Stock branches are searched and the shared semantic contract is agreed. Exchange only research metadata through the approved boundary.

**Question / baseline:** Can a renamed variant or reused dataset appear to be an unrelated first test? Can a researcher choose spending after seeing the result? Compare the current resolution ledger to precommitted records with canonical family, sequence, configuration and data-contact identity.

**Invariant / benchmark:** Config changes create a new experiment; spent information cannot reset by renaming; alpha/e-wealth rules are frozen before outcomes. Search pressure is descriptive metadata, not a fabricated probability. Benchmark registration, duplicate checks and lineage reconstruction at growing experiment counts.

**Prospective validation / deletion rule:** Audit an actual campaign for complete allocations and data contacts. Remove duplicate ledger storage; preserve existing identities through migrations. Advanced online-FDR procedures remain conditional on supported stream assumptions, not names in a roadmap. **Authority: NONE.**

### B3 — Family resampling and continuous monitoring, one verified method at a time

**Existing modules reused:** `sequential_evidence.py`, `confidence_sequence.py`, `strategy_edge_cs.py`, `feature_family_selection.py`, `selection_adjusted.py`, `statistical_universe.py`, hypothesis registry. Stock Finder also has run-cluster/placebo inference; inspect it before adding shared resampling utilities.

**Missing capability:** A verified dependence-preserving candidate-family Reality Check/SPA implementation; sequential bounded-mean/paired comparison primitives beyond current Bernoulli monitoring; mathematical tests of the current e-process assumptions. LORD/SAFFRON are later alternatives only if hypothesis-stream dependence supports their guarantees.

**Files to modify:** Existing statistical and sequential modules, plus the research-only audit CLI. **New files justified:** A small family-bootstrap module only if no reusable implementation exists; its role differs from PBO selection-rank analysis. Do not call a generic maximum bootstrap “Hansen SPA.”

**Question / baselines:** Does the selected candidate beat the benchmark after all registered trials and temporal dependence are included? Does repeated inspection preserve the declared error guarantee? Baselines are full-family null simulations, known alternatives, fixed-horizon tests and existing sequential procedures.

**Invariant / benchmark:** Include unselected candidates; preserve synchronized periods; predeclare block-length sensitivity and family membership; use conditional-null assumptions for e-processes. Exhaustive short-path expectation/stopping checks and seeded dependence controls precede empirical use. Bound bootstrap work and ledger replay cost off-process.

**Prospective validation / deletion rule:** Monitor only a frozen campaign with intact allocation and maturity order. Keep the simplest supported method; remove an estimator or headline guarantee when its assumptions cannot be established. **Authority: NONE.**

## Program C — Randomized shadow science

### C0 — Strengthen existing randomized assignment and causal evaluation

**Existing modules reused:** `research_experimentation.py`, `research_randomization_attestation.py`, `crossfit_provenance.py`, `counterfactual_learning_efficiency_v2.py`, `sequential_learning_ope.py`, `realized_learning_yield.py`, `causal_allocator_calibration_v2.py`. Internally attested cluster/wave randomization, known propensities, AIPW, ESS, censoring checks and certified held-out predictions already exist.

**Missing capability:** Unified estimand/analysis manifest, IPW/SNIPW reference comparisons, clipping sensitivity, complete overlap diagnostics, and explicit non-identifiability rather than heroic point estimates. Prefer the certified V2 path; do not create another versioned copy of the entire evaluator.

**Files to modify:** Existing assignment/evaluation/report modules; `test_counterfactual_research_v31.py`, `test_causal_meta_intelligence_v32.py`, `test_truth_yield_v30.py`. **New files:** None initially.

**Question / baselines:** In a preregistered disagreement region, does deep shadow evaluation improve matured held-out prediction quality per cost versus light evaluation/control? Compare randomized mean difference, IPW, SNIPW and AIPW on identical assignments. Trade profit is not the allocator reward.

**Invariant / benchmark:** Assignment precedes outcome, is immutable, and cannot be rerolled; cluster correlation is respected; held-out truth does not enter nuisance fitting; unsupported overlap returns `NOT_IDENTIFIABLE`. Test both nuisance-correctness cases, tampering, future propensities, missing support and differential censoring. Measure ledger and batch evaluation cost outside runtime.

**Prospective validation / deletion rule:** Run a frozen shadow campaign with real matured research outcomes. Retain causal complexity only if it improves uncertainty/calibration or research choices over randomized direct comparisons. **Authority: NONE.**

### C1 — Heterogeneous research effects only after supported randomization

**Reuse / files:** Extend the same certified causal evaluator, predefined regime metadata and feature stability reports. **Missing:** Supported subgroup effects with held-out orthogonalized estimation. **New files:** None until a simple interaction model fails a defined need.

**Question / baseline:** Does enrichment value differ by a predefined liquidity/session group beyond sampling uncertainty? Compare pooled treatment effect and regularized interactions before causal forests.

**Invariant / benchmark:** Treatment overlap and independent cluster support within each group; no observational feature importance labeled causal. Bound fit/evaluation cost. **Prospective validation:** Confirm subgroup differences in a later randomized wave. **Deletion:** Remove unsupported specialists or interactions. **Authority: NONE.**

## Program D — Market-state and information-flow research

### D0 — Causal state posterior against deterministic regimes

**Existing modules reused:** `regime_stability.py`, `regime_mixture.py`, `bayesian_changepoint.py`, `contextual_ensemble.py`, `causal_features.py`, `incremental_oof_value.py`. These implement temporal regimes and a Bayesian Bernoulli split mixture, not an HMM/Kalman/particle market filter.

**Missing / files:** A filtering posterior, transition hazard and proper predictive scores. Extend existing regime evaluation and feature provenance; a new `latent_market_state.py` is justified only for genuinely distinct filtering mathematics. Start with one parsimonious filter, not three model families.

**Question / baselines:** Does a state posterior improve next-session prediction/calibration beyond fixed regimes and an exponentially weighted simple model? Score online predictions before labels; no hindsight smoothing.

**Invariant / benchmark:** Appending future events cannot change an earlier posterior; probabilities normalize; missing/unstable support is explicit. Measure batch/stream research latency without importing the model into control.

**Prospective validation / deletion rule:** Frozen prequential log score and matched prediction value across predefined sessions, with protected coverage/tail metrics. Delete the filter if it cannot exceed the simple model's practical threshold. **Authority: NONE.**

### D1 — Event intensity and asynchronous lead/lag

**Reuse / files:** `microstructure.py`, `microstructure_windows.py`, `execution_attribution.py`, `causal_features.py`, `feature_stability.py`, `feature_family_selection.py`. Extend rate features and temporal provenance first. A separate `event_intensity.py` is justified only after a Hawkes-family challenger is warranted; asynchronous covariance belongs in an isolated research function with explicit timing inputs.

**Missing:** Verified event-intensity models, asynchronous lead/lag protocols and multiple-testing-governed information-flow claims.

**Question / baselines:** Does trailing quote-update intensity improve 1-second adverse-markout prediction after spread, underlying return and session controls? Start with counts/rates and regularized logistic regression. Hawkes, transfer entropy or cross-excitation graphs must beat that baseline; predictive direction is not structural causality.

**Invariant / benchmark:** Event/receive clocks, sequence and coverage support the claimed ordering; no naive forward-fill manufactured lead. Test asynchronous nulls, timing uncertainty, alternate sampling and future-data injection. Budget per-event/batch work.

**Prospective validation / deletion rule:** Confirm direction, lag and incremental value on later frozen sessions under the registered family correction. Delete unstable relationships and unnecessary graph/model machinery. **Authority: NONE.**

## Program E — Options-market truth before valuation complexity

### E0 — Financial consistency and disclosed IV/Greeks

**Existing modules reused:** `contract_terms.py`, `instrument_identity.py`, `tick_validation.py`, `quote_tape.py`, `microstructure.py`, `feed_integrity.py`, `provenance.py`. `pricing.py` contains locked order-limit/dislocation rules and must retain that meaning.

**Missing / files:** Economic quote validation, model eligibility, bracketed IV and basic Greeks. New `option_market_quality.py` and `option_valuation.py` are justified research-only roles: no current module performs chain consistency or theoretical valuation. Reuse existing identity/terms/timing inputs rather than defining another contract schema.

**Question / baselines:** Which independently confirmed quote defects survive freshness/crossed-quote checks but fail valid financial-consistency bounds? Does adding the diagnostic improve data-quality precision without rejecting legitimate markets? Compare technical-only validation first.

**Invariant / benchmark:** Use executable bid/ask relationships and synchronized inputs; disclose exercise style, rate, dividend, mark and timestamps. American exercise/adjusted deliverables/unknown inputs can invalidate a European formula. Never silently repair prices. Validate external numerical vectors and independent implementations, not only price-IV-price round trips. Benchmark batch chain processing off-process.

**Prospective validation / deletion rule:** Adjudicate new flagged quotes and track false positives/support. Basic valuation correctness is not evidence of alpha. Remove unsupported exotic Greeks or redundant checks. **Authority: NONE; no contract switching.**

### E1 — Surfaces and P&L attribution only with sufficient chains

**Reuse / files:** Extend E0 outputs and `execution_attribution.py`; a research-only `volatility_surface.py` is justified if sufficient chain observations support fitting. **Missing:** Validated surface support/uncertainty, skew/term structure, relative-value and directional/gamma/IV/theta/residual attribution.

**Question / baseline:** Does a constrained surface identify defects or explain shadow P&L more accurately than neighboring-strike interpolation and basic delta/theta attribution? Sparse chains return `SURFACE_INSUFFICIENT`.

**Invariant / benchmark:** Assumptions and residuals remain visible; no arbitrage claim based on asynchronous mids; parameters must be stable under perturbation. Bound optimization and decline unstable fits. **Prospective validation:** Frozen next-chain quality and attribution reconciliation. **Deletion:** Retire surface complexity when interpolation preserves its value. **Authority: NONE.**

## Program F — Execution realism

### F0 — Extend microstructure features and existing markouts

**Existing modules reused / files:** `microstructure.py`, `microstructure_forensics.py`, `execution_attribution.py`, `execution_meta_labels.py`, `fill_calibration.py`, `liquidity_capacity.py`, `latency_value.py`. Configurable markout horizons, fill envelopes, capacity scenarios and latency-value analysis already exist. **New files:** None initially.

**Missing:** Explicitly distinct microprice, static depth imbalance, dynamic event-level OFI and signed trade flow; stronger markout support and transaction-cost decomposition.

**Question / baselines:** Does dynamic OFI improve short-horizon adverse-markout calibration beyond spread and static imbalance? Compare paired identical selections; extend existing markout horizons only where timestamp/data resolution supports them.

**Invariant / benchmark:** No invented queue position; no counterfactual fill labeled observed; no missing horizon converted to zero. Test publisher/sequence order, depth absence and signed-spread identities. Benchmark tape lookup if it changes; optional analysis remains off-process.

**Prospective validation / deletion rule:** Freeze features and measure actual shadow outcomes, execution coverage and cost components. Remove redundant depth/flow features. **Authority: NONE.**

### F1 — Competing fill/adverse/cancel hazards

**Reuse / files:** `opportunity_survival.py`, `fill_calibration.py`, `execution_meta_labels.py`, existing forensic tape. New `execution_hazards.py` is justified because competing fill risks differ from current Kaplan–Meier opportunity survival.

**Question / baseline:** Does a simple discrete-time competing-risk model improve fill and adverse-move calibration relative to current envelopes and a spread/time-only model? Valid order-at-risk timestamps, censoring labels and observed fills are prerequisites.

**Invariant / benchmark:** Cumulative incidence cannot exceed one; censoring is explicit; uncertainty about queue position remains an interval. Compare cancellation policies event by event with identical selection. Bound batch training/scoring and record sensitivity to latency/depth assumptions.

**Prospective validation / deletion rule:** Score frozen hazard predictions against matured shadow observations; retain only improved calibration/practical execution realism. A model-derived cancellation remains a recommendation. **Authority: NONE.**

## Program G — Distribution shift and robust evaluation

### G0 — Diagnose shift beyond one scalar alarm

**Existing modules reused / files:** `distribution_robust.py`, `shift_weighted_conformal.py`, `regime_stability.py`, `online_drift.py`, `drift_retraining.py`, `feature_stability.py`. Weighted calibration already reports ESS, clipping and concentration. New `distribution_shift.py` is justified only for distinct two-sample diagnostics and their witnesses.

**Missing:** Multivariate diagnostics, C2ST/energy/MMD comparisons, interpretable witnesses and temporal null calibration.

**Question / baselines:** Does a predefined shift diagnostic predict next-session calibration degradation beyond current Page–Hinkley and regime-mixture changes? Start with simple feature distances/energy or a held-out classifier; fix model/bandwidth choices before evaluation.

**Invariant / benchmark:** Do not leak time/source identity into a classifier test; preserve dependent sampling; distinguish raw N from effective N. Test equal-marginal/different-joint shifts, no-shift controls and concentrated weights. Bound quadratic kernel work or decline oversized batches.

**Prospective validation / deletion rule:** Compare false alarms and useful early warnings in frozen sessions. Remove extra distances that do not change justified decisions. **Authority: NONE.**

### G1 — Robust neighborhoods and worst groups

**Reuse / files:** Extend `distribution_robust.py` and predefined group reports. Its existing bounded-density regime-mixture stress is not Wasserstein DRO. **New files:** None before a minimal independently checked transport function is needed.

**Question / baseline:** Does an explicitly plausible distributional perturbation overturn the claimed advantage? Compare existing bounded-mixture stress, predefined worst groups and simple 1D transport sensitivity before richer DRO.

**Invariant / benchmark:** Radius, loss support and protected groups are predeclared; a robustness radius is not fitted to preserve the preferred result. Verify limiting cases and monotonic deterioration with larger uncertainty sets; cap optimization work. **Prospective validation:** Check whether reported fragility predicts future degradation. **Deletion:** Keep simpler stress diagnostics when richer optimization adds no information. **Authority: NONE.**

## Program H — Portfolio and tail research

### H0 — Economic exposures and stable covariance

**Existing modules reused / files:** `portfolio_cluster.py`, `strategy_overlap.py`, `tail_dependence.py`, `stress_cluster_risk.py`, `hierarchical_shrinkage.py`, `contract_terms.py`; later use validated E0 Greeks. A separate covariance estimator is justified only if its mathematical contract cannot remain small within existing portfolio research.

**Missing:** Certified economic exposure accounting, factor/residual reporting, covariance shrinkage and predictive risk comparisons. Current portfolio paths, session block bootstrap and tail cofailure clusters already exist.

**Question / baselines:** Does shrinkage improve next-session portfolio risk prediction relative to diagonal/equal-correlation and empirical covariance on the same frozen portfolio? Are apparent distinct strategies mostly the same factor exposure?

**Invariant / benchmark:** Multiplier, units, position overlap and P&L reconcile; covariance is well-conditioned/PSD; residual return is not called alpha without its methodology. Use known matrices and sign/scale/quantity mutations. Bound matrix and bootstrap work off-process.

**Prospective validation / deletion rule:** Score frozen risk forecasts and diversification claims in later sessions. Remove unstable optimizer/factor complexity before considering any allocation use. **Authority: NONE; hypothetical risk fractions are not order sizes.**

### H1 — Tail forecasts beyond robust means

**Reuse / files:** `heavy_tail_edge.py`, `stress_cluster_risk.py`, `portfolio_cluster.py`, `tail_dependence.py`, `return_distribution_dominance.py`. A separate EVT module is justified only for POT/GPD tail estimation, which differs from current robust mean diagnostics.

**Question / baseline:** With enough independent exceedances, does EVT improve conditional tail-risk calibration over empirical/session-block bootstrap? Include threshold/parameter uncertainty, exception frequency and clustering, drawdown duration and joint stress.

**Invariant / benchmark:** Small or unstable tails return insufficient; expected-shortfall components use the same portfolio tail scenarios; synthetic stress is not alpha evidence. Test threshold sensitivity and known heavy/light-tail controls; bound resampling/fitting.

**Prospective validation / deletion rule:** Backtest frozen diagnostic risk forecasts forward. Delete EVT/copula complexity when empirical scenarios provide equally reliable warnings. **Authority: NONE.**

## Program I — Replay and bounded formal verification

### I0 — Unified replay scenarios with explicit observed/simulated truth

**Existing modules reused / files:** `replay.py`, `semantic_replay.py`, `processing_order.py`, `state_checkpoint.py`, `microstructure_forensics.py`, `quote_tape.py`, `production_chaos_drills.py`, `provenance.py`. **New files:** Only a narrow scenario manifest/runner if existing forensic orchestration cannot represent the necessary synchronized streams.

**Missing:** Cross-stream scenario identity, observed-versus-simulated provenance and explicit hypothetical-order model identity.

**Question / baseline:** Which invariant or execution assumption breaks when the same fixed session is replayed under measured p95 latency, feed loss or storage delay? Compare unchanged observed replay to one perturbation at a time before combined faults.

**Invariant / benchmark:** Never merge simulated market reactions/fills into empirical outcome tables. Exact observations, ordering, policy and model identity reconstruct every scenario. Measure deterministic replay throughput, memory and recovery distributions.

**Prospective validation / deletion rule:** Add actual incidents as replay fixtures and verify that subsequent releases reject the failure sequence. Remove duplicate simulators or unrealistic unsupported scenarios. **Authority: NONE.**

### I1 — State exploration, authority reachability and mutation

**Reuse / files:** Existing control transitions in `delivery.py`, `fail_safe_control.py`, `authorization.py`, deployment core, scanner boundary and Program A replay tests. Add separate bounded model/spec and safety-mutation tests; do not rewrite production transitions in a new framework.

**Missing:** Explicit state-space exploration, forbidden authority reachability evidence, TCB inventory and mutation protection tied to named promises.

**Question / baseline:** Can bounded exploration find a halt/prepare/lease/ack or failure/retry ordering the ordinary fixtures miss? Can weakening halt, allowlist, duplicate or temporal gates survive tests? The baseline is current invariant coverage and minimized known counterexamples.

**Invariant / benchmark:** No halted new authorization, duplicate logical dispatch, scanner authority escalation, skipped failed predecessor or ack-before-prepare. Static Python imports alone do not prove capability isolation. Disclose model bounds and explored-state count; keep solver/model checker off the hot path.

**Prospective validation / deletion rule:** Every discovered sequence becomes an integration regression; future incidents challenge coverage. Keep the smallest effective specification and remove redundant models, never the safety promise. **Authority: NONE.**

## Program J — Govern research evolution and complexity

### J0 — Extend existing candidate/evidence lineage, not a parallel Forge

**Existing modules reused / files:** `evolution_ledger.py`, `evolution_controller.py`, `governance.py`, `research_hypothesis_memory.py`, `research_breakthrough.py`, `research_insight_outbox.py`, `provenance.py`, `dataset_fingerprint.py`, `production_gate.py`, `release_guard.py`. These already provide immutable challengers, negative memory, evidence gates, human review and release ancestry.

**Missing:** Consume B2 preregistrations through a small research compiler; track experiment dependencies/data contamination, claim aging/contradiction and concise evidence explanations. **New files:** A dependency/manifest validator only if it cannot remain an extension of the existing hypothesis and provenance modules. No second Candidate Forge, Evidence Tribunal or evolution ledger.

**Question / baseline:** Can a downstream experiment proceed after its foundational data claim failed, or can a candidate silently change endpoints/configuration after evaluation starts? Compare current manifests and review eligibility with enforced registered execution identity.

**Invariant / benchmark:** Generated hypotheses enter an idea/proposed state; sealed labels remain inaccessible; conflicting evidence is preserved; no candidate self-approval. Benchmark dependency checks, cold claim reconstruction and ledger growth.

**Prospective validation / deletion rule:** Audit real campaigns for reproducible registration-to-code execution and fewer repeated failed ideas. Remove duplicate registries and unsupported automatic conclusions. **Authority: NONE.**

### J1 — Research allocation and measurable regret

**Reuse / files:** `information_value.py`, `realized_learning_yield.py`, `learning_path_planner.py`, `causal_allocator_calibration_v2.py`, C0 assignment/evaluation. Existing exact knapsack and causal learning-path planning are the incumbent; they are not already Thompson sampling or UCB.

**Missing:** Bounded exploration, explicit alternatives/cost/latency and a research-regret ledger. A small bandit policy may extend the allocator only after the reward and feedback design is credible.

**Question / baselines:** Does bounded exploration produce more independently validated research information per cost than current knapsack, round-robin and recency allocation while preserving family coverage? Reward labels, uncertainty reduction and validated research usefulness—not live trade profit.

**Invariant / benchmark:** Only compute, labels, provider queries, reviews and experiment slots are allocated; logged propensities and delayed feedback remain intact. Measure full research cost and allocator latency, including wasted work.

**Prospective validation / deletion rule:** Run a frozen randomized allocation campaign evaluated by C0. Remove the bandit if deterministic allocation performs as well within the practical margin. **Authority: NONE.**

### J2 — Component fitness, simplification and deprecation

**Reuse / files:** `incremental_oof_value.py`, `pareto_selection.py`, `ensemble_diversity.py`, `feature_stability.py`, `evolution_ledger.py`, governance/reporting. **Missing:** Periodic marginal contribution, reliability/latency/dependency cost, non-inferiority criteria for simplification and reviewable deprecation recommendations. **New files:** None initially.

**Question / baselines:** Does each optional component improve protected prospective metrics enough to justify its measured cost? Compare paired ablations, unchanged incumbent and a simpler substitute; predeclare non-inferiority margins.

**Invariant / benchmark:** Average gains cannot hide catastrophic groups, reduced coverage or execution failure; Pareto comparisons account for uncertainty. Removing a safety/control obligation is not model pruning. Report actual runtime/dependency/configuration/TCB deltas.

**Prospective validation / deletion rule:** A sufficiently supported component with zero practical marginal value becomes a human-reviewed deletion candidate. A complex teacher may be retained for research while a simpler verified challenger is proposed. No component graduates permanently, and no automated process promotes itself. **Authority: NONE.**

## Sequencing and review exits

The first execution sequence is **A0/A1 → attested baseline → B0**. B1 and B2 follow as separate changes. B3 requires B2's family/data-contact evidence. C0 strengthens existing infrastructure before new causal claims. E0 is a data-truth prerequisite for surface features and Greek-based portfolio work. D/F require trustworthy event tapes; H requires coherent portfolio accounting; J1 requires valid randomized feedback. J0 can organize these dependencies after the baseline without granting research authority.

For every increment, the PR description must state the reused surfaces, exact changed/new files, counterexample or registered question, protected invariant, test/benchmark evidence, prospective status, complexity deltas and deletion path. Distinguish **implemented**, **tested for correctness**, **historically evaluated**, **prospectively evaluated**, and **operator-review-ready**. None is synonymous with live deployment.

No aggregate APEX milestone is complete merely because every row has code. The accepted outputs are narrower: an invalid claim correctly rejected, a reproduced failure permanently blocked, an independently confirmed data defect detected, a frozen prospective improvement established, or unnecessary complexity removed without losing validated value.

## Method references for the first statistical increments

- Bailey, Borwein, López de Prado and Zhu, [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf), Algorithm 2.3: synchronous candidate matrices, CSCV and `rank/(N+1)` relative ranks.
- Bailey and López de Prado, [The Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf): non-normal returns, sample length, trial-Sharpe variance and independent trial count. An implementation must disclose those inputs and their assumptions.

New mathematical methods require their own primary-source review and independent numerical tests before implementation. This map does not pre-certify future formulas.

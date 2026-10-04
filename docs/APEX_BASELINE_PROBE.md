# Program A baseline probe

Frozen capture method: `docs/apex/program-a/probes/apex_baseline_probe.py.gz`. Documentation: `docs/APEX_BASELINE_PROBE.md`.

This offline helper captures source identity, finite semantic behavior and explicitly scoped latency workloads from one requested worktree per fresh interpreter. It calls the existing parser, association, reducer, Store, replay and benchmark APIs. It does not start Discord, use brokerage credentials or run advanced quantitative research. Its output supplies evidence for the baseline attestation; it does not approve a discrepancy or close the baseline gate.

## Current verification snapshot

The MFF control reconciliation has been published through PR3–PR6. The following original and reconciled source heads were captured using the same helper and golden fixture:

| PR | Original source SHA | Published reconciled source SHA | Reconciled full-suite tests passed | Literal-head CI |
|---|---|---|---:|---|
| 3 | `bfa36a3a1e5512b18e3e3906e535231fd9a65805` | `00c594fb2b9b477c8ae9286d8aa2d4d6f64cfcf5` | 78 | Green |
| 4 | `35d37388fc3b182f2e167c9a6abdecf66bcf6c45` | `8f41fadac3bcac545246119aca08e83fed4a9fa4` | 168 | Green |
| 5 | `79faaabf1ff33e37c8d31b72b28ef2744d478598` | `c75afb7b08730e7cc7cd6fba67760d550645e67d` | 482 | Green |
| 6 | `9f17d43f3f70d508278370b33e44b014d3cadfa4` | `7942c0ff59689c7d11a78c972c22a65da7c54626` | 536 | Green |

These suite totals overlap across branches and must not be summed as independent coverage. The helper does not run pytest or query GitHub CI; the table incorporates the separate Program A verification records. The final attestation must cite the corresponding literal-head CI runs.

Eight completed historical captures use collection paths `apex-evidence/baseline-{original,reconciled}-pr{3..6}`. Each contains `baseline.json`, `benchmark.json` and 40 case traces. The four corresponding `apex-evidence/mff-pr{3..6}-semantic-diff.json` files report:

- Stable source, helper and corpus identities.
- Zero changed cases in each original-to-reconciled pair.
- 39 complete cases and one `malformed_expiry` case with an unexpected February-30 parser `ValueError` on every head.
- Zero golden-kind mismatches; the two invalid-fill exceptions are expected defensive outcomes.
- Comparison status `IDENTICAL_WITH_INCOMPLETE_CASES`, not an all-cases-passing verdict.

**The cross-repository baseline gate is BLOCKED_COMPANION_RESEARCH_CI.** Stock Finder's research-line full-suite CI has exposed evidence-reproduction and report-reproducibility failures. Completed MFF captures and green MFF CI do not clear those failures or supersede the consolidated [APEX_BASELINE_ATTESTATION.md](APEX_BASELINE_ATTESTATION.md). No new APEX mathematics begins merely because these captures exist.

## Reproduce a capture or comparison

Run the commands from the repository root, with the original and reconciled checkouts in sibling directories and a separate evidence directory. Verify the evidence pack, then extract the frozen helper bytes. `tools/verify_apex_evidence.py` is the only new runnable utility in the baseline documentation PR; there is no second edited `tools/apex_baseline_probe.py`.

```bash
python tools/verify_apex_evidence.py
```

Continue only when the verifier reports `artifact_integrity=VERIFIED`. That result verifies manifest-bound artifact bytes; it does not rerun CI, approve incomplete semantic evidence or change the recorded baseline gate.

```bash
mkdir -p ../evidence
gzip -cd docs/apex/program-a/probes/apex_baseline_probe.py.gz \
  > ../evidence/apex_baseline_probe.py
```

Use the same interpreter, frozen golden fixture and extracted helper for a comparison. Choose a new output directory for each run; the helper does not protect an existing output directory from overwrite. Preserve the eight baseline captures above.

```bash
python ../evidence/apex_baseline_probe.py capture \
  --repo ../mff-original-pr3 \
  --out ../evidence/rerun-original-pr3 \
  --golden ../mff-original-pr3/tests/fixtures/mff_golden.json \
  --mode all --samples 320
```

Run separate sequential commands for the other original and reconciled worktrees, changing `--repo` and `--out`. Verify that each checked-out SHA matches the intended capture; a branch name can move. **Do not run timed captures in parallel, during tests or while other local agents are doing compute-intensive work.** `--mode semantics` captures only semantic behavior; `--mode benchmarks` captures only benchmarks. In `--mode all`, semantics run before the timed workloads.

```bash
python ../evidence/apex_baseline_probe.py capture \
  --repo ../mff-pr3 \
  --out ../evidence/rerun-reconciled-pr3 \
  --golden ../mff-original-pr3/tests/fixtures/mff_golden.json \
  --mode all --samples 320

python ../evidence/apex_baseline_probe.py compare \
  --before ../evidence/rerun-original-pr3/baseline.json \
  --after ../evidence/rerun-reconciled-pr3/baseline.json \
  --out ../evidence/rerun-pr3-semantic-diff.json
```

The recorded captures share these reproducibility identities:

| Artifact | Identity |
|---|---|
| Probe version | `apex-baseline-probe-v1` |
| Probe SHA-256 | `0a3d5be865f021629054838e43278fc5dad270c70da8f98bd4c1d65ebe105633` |
| Corpus SHA-256 | `dc27c31bc0e18ef28f289945be92ebe09e72986367ef04f128085f357291d1a5` |
| Frozen original-PR3 golden fixture SHA-256 | `e0ea0eb38131dfb959554c6f26d13d2a712c48ef0fba14e65a684db357def5ea` |
| Synthetic fixture time | `2026-09-08T14:00:00+00:00` |

The fixture time is evidence input, not the wall-clock run time. Every `baseline.json` also records the exact source SHA/tree, source-file hashes, locked-strategy fingerprint, parser source/version and available runtime-policy identity. Historic absolute paths are provenance, not dependencies for a rerun: provide the current checkouts through the CLI. Preserve the helper bytes if reproducing the recorded probe hash. The compressed raw method is retained unchanged for that purpose; it is not represented as a globally lint-certified production component.

Semantic and benchmark stores are isolated `TemporaryDirectory` artifacts and are removed on normal completion. JSON traces, source fingerprints, counts and common-path raw timings remain. Database/WAL forensic snapshots are not part of the retained evidence contract. The source worktrees are not edited by the helper.

## Semantic evidence and health

The fixed corpus combines the existing 15 golden parser examples with blocked guild/channel/author, Friday, NKE, negation, conditional language, duplicates, two-position/first-pipe limits, repeated trims, once-only add, valid and invalid fills, normalized operator STOP, source stop-loss follow-up, edits, replies and out-of-order source timestamps.

The helper retains parser and associated events, complete parser evidence except `latency_us`, association evidence, ordered effects, full effect multisets, every state transition, native state fingerprints and separate native default/input-order replay. The omitted latency fields are named in each case. Fills are synthetic accounting fixtures; they are not observed executions or evidence of fill feasibility. Native replay is marked inapplicable for fill-containing scenarios because the normalized replay API has no fill-event schema.

Semantic association uses the worktree's actual `Store.append_signal()` and `Store.contract_for_message()` methods. Received-time and revision lookup behavior is not approximated by a last-seen dictionary. Native replay retains its own ordering semantics separately, so an ordering difference remains visible rather than being normalized away.

`capture_status=VALID_CAPTURE` means the source SHA, source contents, helper and golden fixture remained unchanged. **It does not mean the semantic cases passed.** Read `semantics.status`, `incomplete_cases`, `golden_mismatches` and each case's `semantic_status`. A zero capture exit code also does not certify semantic health.

Two invalid-fill scenarios declare expected defensive exceptions. All other exceptions, including invariant errors, record their failing stage and mark unexecuted downstream coverage as incomplete. The shared `malformed_expiry` failure is a baseline residual; reproducing it exactly does not convert it into an expected success. Its repair, if undertaken, needs a named regression and an explained semantic difference.

Differences are classified as parser/action/evidence, association, contract/event, effect, state, fill, identity/fingerprint and replay capability/ordering changes. Identity changes remain visible even when economics match. A different helper or corpus, or a source changing during capture, makes the comparison `NOT_COMPARABLE`. The compare command writes a report and returns zero even when the report requires review; automation must inspect the report's `status` and case details.

The finite corpus characterizes parser/association/reducer behavior. Runtime admission, durable halt, scanner authority, sentinel corruption response, dispatch boundaries and recovery require their separate invariant suites. Equality on this corpus is not a global equivalence proof or complete state-space exploration.

## Benchmark workloads and measurement boundaries

The shared helper supplies two workloads that are comparable across all eight source heads. Native worktree benchmarks are additional, separate evidence.

| Report field | Measured workload | Scope limit |
|---|---|---|
| `comparable_core_compute` | Valid configured guild/author/channel; parser → association → reducer → invariant, cycling one QQQ entry, an ambiguous follow-up and NKE rejection | Repeated pending-contract state; no fills, durable ingress, decision-packet admission, quote acquisition or broker/network work |
| `comparable_durable_pipeline` | `await Pipeline.handle()` from invocation through durable return, with valid guild/author, deliberately rejected channel and distinct revisions | Rejected-channel control workload with no positions or effects; excludes Store/Pipeline startup and broker/quote/network work |
| `native_hot_path_benchmark` | The worktree's unmodified `benchmark_hot_path()` API, where present | Different implementation, internal timing boundaries and native benchmark generations; retain as its own series |

Both common workloads use `time.perf_counter_ns()` and preserve every measured duration. All eight common-core captures verified 107 `ENTRY`, 107 `AMBIGUOUS` and 106 `IGNORE` events. All eight durable captures verified 320 raw revisions, 320 normalized signals and zero proposed effects. These checks prevent a silently changed workload from looking faster.

No measured warm-up samples are discarded. This is not a cold-start claim: `--mode all` has already imported modules and run semantic cases. Durable startup is outside the timing window. The first measured call and the head's actual sentinel cadence remain in the samples.

Original PR3 and every reconciled head report a real sentinel object, cadence 64 and a successful-commit counter moving from 0 to 320. Original PR4–PR6 report the sentinel absent. This records its configured participation in the timed path; it is not a separate sentinel timing profile or proof of corruption detection and halt behavior. Use the sentinel fault tests for those properties.

### Native v1 and v2 are different workloads

The original PR5/PR6 `hot-path-benchmark-v1` fixture uses the wrong guild. Its core inputs are all `IGNORE`; the original implementation does not report an event-kind histogram. Its source fingerprint is `76d4849f314bf2f71805722f5e375f05b64ad0cf98886b1b3a8ad9dc6733a867`.

Reconciled PR5/PR6 use `hot-path-benchmark-v2`, the configured guild and explicit core-kind accounting. The captured native histogram is 107 `ENTRY`, 107 `AMBIGUOUS` and 106 `IGNORE`; its source fingerprint is `59a24d87e31f8e20c9ddf80bb04fc6f565eb15cfceecbb1c6fc129647dcbc755`.

**Do not use native v1-to-v2 timings for an equal-work parser comparison.** Use `comparable_core_compute` and `comparable_durable_pipeline`. PR3/PR4 have no native `hot_path_benchmark.py` and report `UNAVAILABLE_IN_THIS_HEAD`. None of these measurements is a source-to-live-order latency claim.

## Captured common-path latency

Each row contains 320 samples from a sequential capture while local agents were paused. Percentiles use nearest rank, `sorted[ceil(q*n)-1]`. The two tables preserve the complete available summary; `benchmark.json` contains all raw common-path nanosecond durations.

### Common core compute — microseconds

| Capture | p50 | p90 | p95 | p99 | p99.9 | Max |
|---|---:|---:|---:|---:|---:|---:|
| Original PR3 | 18.467 | 23.626 | 30.696 | 57.185 | 2035.872 | 2035.872 |
| Reconciled PR3 | 18.588 | 24.617 | 32.729 | 123.824 | 140.929 | 140.929 |
| Original PR4 | 29.134 | 35.152 | 57.446 | 94.340 | 141.119 | 141.119 |
| Reconciled PR4 | 29.674 | 36.705 | 48.622 | 101.381 | 136.122 | 136.122 |
| Original PR5 | 37.014 | 45.037 | 68.332 | 129.352 | 238.794 | 238.794 |
| Reconciled PR5 | 29.724 | 38.327 | 53.530 | 111.476 | 163.853 | 163.853 |
| Original PR6 | 38.067 | 68.191 | 107.129 | 211.304 | 241.137 | 241.137 |
| Reconciled PR6 | 30.465 | 84.185 | 124.926 | 202.371 | 237.012 | 237.012 |

### Common durable control path — milliseconds

| Capture | p50 | p90 | p95 | p99 | p99.9 | Max |
|---|---:|---:|---:|---:|---:|---:|
| Original PR3 | 1.233 | 1.429 | 1.817 | 4.576 | 10.923 | 10.923 |
| Reconciled PR3 | 1.594 | 2.256 | 2.974 | 6.124 | 48.863 | 48.863 |
| Original PR4 | 1.564 | 1.918 | 2.061 | 3.591 | 86.566 | 86.566 |
| Reconciled PR4 | 1.880 | 2.313 | 2.676 | 5.508 | 8.792 | 8.792 |
| Original PR5 | 1.626 | 2.329 | 2.454 | 5.536 | 15.838 | 15.838 |
| Reconciled PR5 | 2.087 | 2.835 | 4.298 | 7.152 | 9.370 | 9.370 |
| Original PR6 | 1.755 | 2.334 | 2.705 | 5.782 | 14.970 | 14.970 |
| Reconciled PR6 | 2.382 | 3.509 | 5.946 | 10.146 | 38.040 | 38.040 |

The top MFF source's durable p50/p95/p99 increased from **1.755/2.705/5.782 ms** to **2.382/5.946/10.146 ms** in this batch. This is an observed cost, not a speedup. It cannot be attributed entirely to one change from a single batch, nor translated into economic loss without an execution-value study. The original upper heads omitted lower-level controls now present in the reconciled workload.

At 320 samples p99.9 equals the maximum. These values do not estimate production p99.9 reliably. If tail latency determines an engineering decision, collect separately identified isolated repetitions before drawing a causal conclusion; preserve the existing measurements.

## Environment, validation and retained limitations

The captures record AMD EPYC 9V74 CPU identity, nine available logical CPUs/affinity entries, Linux `6.18.44` on x86-64 with glibc `2.39`, Python `3.13.15`, SQLite `3.53.1` and `CLOCK_MONOTONIC` with a reported 1-ns clock resolution. The mounted filesystem is `overlay`; physical storage media is `UNKNOWN_UNLESS_EXPOSED_BY_MOUNT_SOURCE`. Clock resolution is not measurement accuracy. Full environment, filesystem capacity and before/after load averages are retained in each `benchmark.json`.

Local agents were paused for the recorded capture window. Host-wide competing workload was not measured or controlled; the environment record explicitly says `NOT_MEASURED`. These are environment-specific observations, not certified deployment latency.

Requested benchmark samples below 300 are rejected. A core-kind mismatch or unexpected effect fails capture. Durable counts must equal the requested sample count and proposed effects must be zero; a count mismatch is saved as invalid evidence and causes a nonzero exit. Source mutation also causes a nonzero exit after the evidence is saved. No success status should be inferred from a partially written directory after an exception.

PRAGMA readings are connection-specific. The common durable report queries one actual `Store.connect()` connection after the loop. Original PR4–PR6 have `foreign_keys=0` there; original PR3 and all reconciled heads have `foreign_keys=1`. All captured common Store connections report WAL, `synchronous=2` and a 5000-ms busy timeout. The native helper's synchronous-mode observation comes from a separate bare SQLite connection. **Neither observation certifies every writer connection.** The runtime connection audit and dedicated per-connection tests supply that broader evidence.

This helper provides no empirical trading performance, prospective selection lift, fill realism, all-path authority proof or complete recovery certification. Those require separately named datasets, tests and protocols. The current incomplete malformed-expiry case, scoped latency increase and remaining cross-repository attestation work must stay visible when these results are summarized.

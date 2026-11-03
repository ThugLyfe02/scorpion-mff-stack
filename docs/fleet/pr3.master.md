# PR #3 — the semantic lane

**Branch:** `hardening/calibrated-semantic-acceleration` · **Worktree:** `.worktrees/pr3`
**Continuation brief for `src/scorpion/` semantic modules.** Read `master.prompt.md` first.

Measured baseline on 2026-10-29:

```text
drift vs main     7 ahead, 51 behind        <- the lane is a stale stack, not a candidate
base PR #2        0 commits unique to #2; #3 already contains it
gate               ruff clean · mypy --strict clean · 126 passed
authority          review / advisory only; no execution path
```

The 51-commit drift is the first fact of this lane. A certification system that runs on
a tree 51 commits behind the thing it certifies is not evidence; it is a rumour. The
lane's next session starts with convergence, not with a new module.

---

## 1. What this lane is

Interpretation truth. The question is not "did the parser return a value" but "is the
parse calibrated, is the wording in distribution, do independent shadows agree, is the
contract real, is the quote real, and does a candidate parser change the *book* or only
the *labels*".

```text
raw message -> parser + evidence -> semantic envelope (no authority)
                                   |-> review queue (durable, prioritized, SLA)
                                   |     claim -> resolve -> adjudication
                                   |     adjudication -> calibration
                                   |-> certify(candidate parser)
                                         corpus + adjudications
                                         accuracy / precision / ambiguity
                                         ECE / Brier / risk-coverage
                                         McNemar / action escalations
                                         semantic state + effect multiset
                                         -> CERTIFIED or REFUSED (digest)
```

Read before writing: `docs/CALIBRATED_SEMANTIC_ACCELERATION.md`,
`docs/SEMANTIC_CONTROL_PLANE.md`, `docs/PRECISION_INTELLIGENCE.md`,
`docs/ARCHITECTURE_V2.md`, `docs/IMPLEMENTATION_STATUS.md`.

**The lane boundary:** the envelope and the certificate have deliberately **no
execution authority**. They cannot mutate the book, approve an effect, size a position,
or submit an order. The money path stays deterministic in PR #8. A shadow model is a
recording, never a decision.

---

## 2. Invariants beyond the master law

1. **Calibration is measured, not declared.** A rule is `UNCALIBRATED` until
   adjudicated examples say otherwise; `TRUSTED` requires a Wilson lower bound, a
   Brier score, and a calibration gap that survive the sample floor.
2. **Vote share is not confidence.** Two models both saying ENTRY at 0.40 are not
   unanimous evidence. Consensus mass and absolute support are reported separately;
   zero mass never injects `log(0)` entropy or phantom disagreement.
3. **A promotion is a certificate, read back.** Accuracy, actionable precision,
   ambiguity, action escalations, ECE, risk coverage, segment parity, semantic state,
   effect multiset, and a distinct parser are all gates. A gate that cannot be
   evaluated refuses.
4. **An aggregate gain cannot buy a segment regression.** A channel or rule with ≥10
   labels is protected; an underpowered segment is *reported*, never counted as a pass.
5. **Duplicate effects are visible.** Comparisons use **multisets**: a second
   `PROPOSE_TRIM` must not disappear through set deduplication.
6. **Two fingerprints, two jobs.** `state_fingerprint()` proves operational identity
   (event IDs included). `semantic_state_fingerprint()` normalizes parser-version
   IDs so a candidate is not failed for a renaming, while still comparing the full
   deterministic book.
7. **A resolved item is not silently reopened.** A Discord edit arrives as a new
   revision id and therefore a new event id; that is the only reopen path.

---

## 3. Where this lane goes next

### Move 1 — converge, then re-cut the PR (blocking everything else)

The lane contains its base (#2) and is 51 commits behind `main`. Do not add a module on
top of that.

1. Rebase/replay the semantic commits onto current `main` (or merge `main`, then
   resolve by intent, not by file). Keep `src/scorpion` semantics, never revive a
   copied `scorpion_mff/`.
2. Prove the gate on the converged tree; the test count will change, so re-measure and
   update `docs/IMPLEMENTATION_STATUS.md`.
3. Retire **PR #2** as a separate pull request: #3 contains every commit #2 has, and a
   stacked pair that is both stale doubles the rebase cost and splits the proof. One
   semantic lane, one PR, reviewed against `main`.

Acceptance:

```bash
git rev-list --left-right --count main...HEAD   # right side small enough to review
bash scripts/gate.sh                            # green on the converged tree
```

A lane that cannot be reviewed in one sitting is not converged; split the *commits*,
not the lane.

### Move 2 — become the only door a parser change walks through

Today certification is a library and a CLI. It needs a contract with the live lane:

- PR #3 publishes the parser version it certifies and the certificate digest.
- PR #8 pins that digest in its own tests and refuses to adopt a parser without one.
- The certificate's semantic state and effect-multiset gates are the definition of
  "downstream-safe", so a label-only improvement is refused.

Acceptance:

```bash
scorpion-certify --baseline <v> --candidate <v> --corpus <c> --adjudications <a>
# CERTIFIED with digest, or REFUSED with named reasons; load_certificate + verify_certificate
```

- A candidate that flips one IGNORE to ENTRY is refused (`action_escalations`).
- A candidate that adds one effect with identical labels is refused (`effect_multiset_changed`).
- A certificate from another parser pair, a tampered digest, a stale timestamp, or an
  impossible count refuses on read-back.
- PR #8's pinned digest changes only in a commit that cites the new certificate.

### Move 3 — close the human loop with real adjudications

The queue and the active-learning scorer exist; the loop is only real when labels move
a rule.

```bash
scorpion-review --db scorpion.db backfill
scorpion-review --db scorpion.db list --tier URGENT
scorpion-review --db scorpion.db resolve <id> --kind ENTRY --contract ... --reviewer ...
scorpion-review --db scorpion.db metrics
scorpion-accuracy --db scorpion.db
```

Acceptance: a named rule moves `UNCALIBRATED -> PROVISIONAL -> TRUSTED` only through
adjudications and a certificate; the queue reports open-by-tier, SLA breaches, and
per-rule calibration progress; `backfill` never invents a rule, confidence, or label
for an event with no audit.

### Do not add a fourth evidence module

Novelty, ensemble, envelope, review, certification, semantic replay, active learning:
the loop is complete enough to test. The missing input is **adjudications**, not another
scorer. Every new score added before the loop closes is a second opinion nobody asked
for.

---

## 4. What this lane refuses

- Any execution authority, any book mutation, any "auto-approve" path.
- A promotion on aggregate accuracy; a segment regression bought with an average; an
  underpowered segment counted as a pass.
- Certifying the baseline against itself; a certificate without read-back; a digest
  treated as a signature (there is no key management, and the system must not pretend).
- A second parser or a second state fingerprint. A new wording is a rule + fixtures in
  the one parser, not a parallel grammar.
- An LLM in the decision path. A shadow model records; it never decides.

---

## 5. Handoff to the fleet

- **To PR #8:** parser version + certificate digest, and the fixture set that must keep
  passing. A promotion is adopted only when the live tests pin the digest.
- **To PR #1:** the adjudication corpus and semantic fingerprint shapes; the control
  plane must not invent a second definition of identity.
- **To the fleet:** the review queue metrics (open by tier, SLA breaches, resolution
  rate) and the calibration table. These are leading indicators of interpretation risk.

---

## 6. Kill criteria

- The lane adds a module while more than 10 commits behind `main` (convergence is the
  only work).
- A certificate cannot be read back and verified, or names a parser pair it did not run.
- The queue has no resolved adjudications after a full session; the loop is not closed.
- Any change would give the envelope authority over the book. Stop and delete it.

---

## 7. Preflight

```bash
git status -sb
git rev-list --left-right --count main...HEAD
bash scripts/gate.sh
scorpion-review --db scorpion.db metrics    # is there a live queue at all?
```

Then converge. The lane's value is not in how much evidence it can compute; it is in
how little a reviewer has to take on faith.

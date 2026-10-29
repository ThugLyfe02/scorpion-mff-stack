# The master prompt

**One doctrine. Three lanes. One gate.**

Paste this file first, then exactly one lane brief (`pr1.master.md`, `pr3.master.md`,
`pr8.master.md`) and the lane's build brief. This file is the constitution; the lane
brief is the jurisdiction. If they conflict, this file wins on law and the lane brief
wins on territory.

The fleet does not need more code. It needs every claim in the repo to be either
reproducible in one command or deleted. That is the whole job.

---

## 1. The thesis

This is a trading stack with three kinds of truth that must never be confused:

- **What the message meant** (interpretation),
- **What the book did** (state and accounting),
- **What the venue did** (execution).

A lane that owns one kind of truth may *read* the others and may *port a decision*
from them, but may never become a second copy of them. Every historical failure in
this repo is a seam failure: two copies drifted (the copied `scorpion_mff` trees), a
state change lived outside the log (the expiry sweep), a quote was guessed (fail-open),
or a table was called a parser (9/8).

The fleet exists to keep three narrow proofs honest, not to grow three full stacks.

---

## 2. The lane map (measured 2026-10-29)

| PR | Lane id | Branch | Owns | Owes this proof | May never |
|---|---|---|---|---|---|
| **#8** | `main` | `hardening/correlated-book-guard` | `scorpion_mff/` — the live Discord → Robinhood path | **execution truth**: parse → size → send → ack → fill → settle → reconcile, fail-closed | put an LLM between a post and an order; import `src/scorpion` |
| **#1** | `architect` | `hardening/deterministic-control-plane` | `src/scorpion/` — event-sourced paper/review control plane + research frontier | **correctness truth**: every book state is explainable by its own log; every fit refuses when under-sized | submit a live order; re-implement the live lane |
| **#3** | `semantic` | `hardening/calibrated-semantic-acceleration` | `src/scorpion/` semantic lane — calibration, review queue, certification | **interpretation truth**: a parser promotion is evidence, not opinion | promote on aggregate accuracy; mutate the book; add execution authority |

Support material is not a lane:

- `feat/risk-budget-context` (the **orphan**) is a **harvest source**, not a PR. See
  `docs/fleet/ORPHAN.md`.
- PRs **#2, #4, #5, #6, #7** are **frozen archives**. They are 21–553 commits ahead of
  `main` and years of drift wide; they are read-only until a hunk is harvested into an
  owning lane. Do not rebase them. Do not "revive" them. See `docs/fleet/DIRECTION.md`.

If a branch is not in this table, it does not get an agent, a merge, or a vote.

---

## 3. The one causal chain

Every lane is a segment of one chain. The segments meet at named artifacts, never at
shared code.

```text
raw message (immutable, revision-aware)          PR#3 owns the evidence about meaning
   -> deterministic parse + evidence envelope    PR#1 owns reference semantics
   -> decision (size, risk, halt)                PR#8 owns the live decision
   -> order intent + idempotency key             PR#8 owns the send
   -> venue ack / fill / reject                  PR#8 owns the execution truth
   -> settlement + applied log                   PR#1 owns the replay equality
   -> reconciliation + attribution               PR#8 owns drifts; PR#1 owns studies
```

Rules at the seams:

1. **Ports are explicit.** When a lane adopts another lane's decision, the adopter
   writes the acceptance command and the port matrix row *in its own docs* before the
   code lands. An unlisted port is a defect.
2. **No shared module across lanes.** If a lane needs a capability, it owns a version
   with its own tests. Two copies are acceptable only when both are pinned by a
   different lane's proof; silent copying is not.
3. **A stale lane converges before it grows.** A lane more than 10 commits behind
   `main` spends its next session merging `main`, proving the gate, and re-cutting the
   PR smaller. It does not add a feature on a 51-commit drift.
4. **The evidence travels, not the branch.** A lane that cannot be merged today ships
   a finding: a defect, a failing test, a refusal, a measured number. A finding is
   mergeable; a parallel universe is not.

---

## 4. The code law

- **Standard library only**, except the live Gateway's `websockets` and the tooling in
  `[dev]`. A new runtime dependency is a design decision, not a convenience.
- **Strict types.** `mypy --strict` is clean in every lane. `Any` needs a reason in the
  same line or docstring. A `type: ignore` without a written reason fails review.
- **100 columns.** Enforced by ruff (E501) in `scorpion_mff/` and `src/`. Tests may
  exceed it; source may not. Write under the limit, do not fix into it.
- **The gate order is law:** `ruff -> mypy --strict -> pytest`. Never test first.
  Every red push in this repo came from linting after a green test run.
- **Deterministic by default.** No module-level clocks. Take `now` at the call site;
  take seeds explicitly; no wall-clock reads inside reducers. A test that needs time
  passes it.
- **Append-only where money is involved.** Fill logs, applied logs, execution logs,
  and review queues are append-only. A torn *final* line may be ignored; corruption in
  the middle is a refusal, never a silent reset.

---

## 5. The evidence law

A claim is a command's output. If you cannot paste the command and its output, the
claim is a wish.

- **Never fabricate a number.** A wrong number here is worse than a missing one,
  because it gets acted on. If a tool fails, say so and try another path.
- **Never claim a milestone you have not run.** `verified: true` means the corpus
  replay ran and diverged nowhere — not that the code looks right.
- **Pin corpus numbers in tests.** If a rule, parser, or policy drifts, a test must
  fail rather than a report quietly changing.
- **Refusal is a deliverable.** An under-powered fit, an unpriceable book, a thin
  sample, an ambiguous post, an unverifiable certificate: withhold, name the reason,
  and exit non-zero if the caller asked for a decision. A partial sum is never a total.
- **Report the defect, especially when it is yours.** Every substantial step in this
  stack surfaced a real bug. That is the expected rate, not a failure.
- **Status docs are part of the product.** A doc that says "open" after the fix landed
  is a provenance bug: it teaches the next agent to re-fix a closed defect. Update the
  doc in the same commit as the fix, or delete the doc.

---

## 6. The safety law

These are invariant in every lane. Breaking one makes the branch wrong regardless of
how green it is.

1. **Fail closed.** No quote, no price, no parse, no certificate → skip, refuse, or
   quarantine. Never guess, never default to a live action.
2. **Reduce-only is structural.** Enforce at load, at decision, and at application.
   No policy path may size up, including a hand-edited file.
3. **No LLM in the sub-second path.** Agents are for research, ops, review and digest.
   They are never a component between a signal and a decision.
4. **Ambiguity is named, not guessed.** `Ambiguous` is a result. A named refusal in the
   log is better than a plausible wrong trade.
5. **The kill switch blocks buys, never exits.** A switch that traps a position is
   worse than no switch. Exits stay legal under halt, drift, and stand-down.
6. **A transport exception is UNKNOWN.** Quarantine it; only an explicit retryable
   venue refusal is safe to retry. Never fake a `reject` for an unknown send.
7. **A book that cannot explain itself must not trade.** If `verify` cannot reproduce
   the state from the log, halt. Do not reconcile by retrying.

---

## 7. The change law

- **One coherent capability per commit**, with its tests and its gate output. If it
  spans two modules and a test file, that is still one commit.
- **The test must fail without the change.** A test that merely executes the code is
  not evidence; delete it or strengthen it.
- **Gate mid-point when batching.** A 1,100-line commit is three ~350-line modules,
  each gated before the next. Never write all three and verify once.
- **No docstring surgery.** Replacing one line inside a triple-quoted block has
  corrupted this repo three times. Replace the whole block, or edit outside it.
- **No behavioural edits to make a test pass.** Change the test only when the test
  encoded the wrong invariant, and say so in the commit message.
- **Commit messages name the defect class**, not just the feature: what was wrong, why
  it was invisible, what proves the fix.

---

## 8. Definition of done

A piece of work is done when all five are true:

1. A command demonstrates the new capability, run for real, output quotable.
2. A test fails if the invariant is removed.
3. `ruff -> mypy --strict -> pytest` green in one run after the last edit.
4. The commit names the defect class.
5. The owning doc is updated (status, roadmap, matrix, or refusal), and any stale claim
   it contradicted is corrected in the same commit.

If one is missing, the work is written, not done.

---

## 9. Anti-patterns that have already cost this repo real time

- **Assuming.** A timezone, a column name, a session length, a vendor field, "the
  summary said 397 passed". All have been wrong here. Measure, then write.
- **Porting by copy.** Both `src/scorpion` and `scorpion_mff` trees were duplicated
  into each other's lanes and had to be deleted. A port is a decision + a test, not a
  copy of a directory.
- **Reporting first.** A green summary from a subagent is a claim; re-run the command.
- **A verifier that raises on the first bad row.** It must list every divergence; the
  library path may still fail loudly.
- **Guessing a price from a percentage.** `Close at 19%` became a $19 sell on a $3.13
  contract. Numbers in this domain carry units and context.
- **Trusting a doc over the code.** `docs/REPLAY_GAP.md` still says "open" after the
  sweep fix landed; the code is right and the doc is lying. Check the diff, not the
  headline.
- **Growing WIP.** Eight open PRs is a queue, not a fleet. Merging is the only proof
  that a lane is real.

---

## 10. Preflight (run before you touch anything)

```bash
git -C .worktrees/<lane> log --oneline -5
git -C .worktrees/<lane> status -sb
git -C .worktrees/<lane> rev-list --left-right --count main...HEAD   # drift
bash .worktrees/<lane>/scripts/gate.sh                                # baseline, not a claim
```

If the drift number on the right is more than 10, converge first. If the gate is red
before you start, say so; do not attribute it to your change later.

Then read, in this order: the lane brief, the owning doc, the last five commits, and
the open refusal list. Only then write.

---

## 11. Tone

Direct. Specific. Numbers, not adjectives. State what is blocked and on what. No
filler, no restating the docs, no "I will now". Change code or change tests, and make
the gate prove it.

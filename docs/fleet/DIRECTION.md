# Where the three PRs go

**The convergence thesis.** A direction document for the Scorpion MFF stack, written
against the tree as measured on 2026-10-29, not against the story the docs tell.

Companion briefs: `master.prompt.md` (law), `pr1.master.md`, `pr3.master.md`,
`pr8.master.md` (jurisdiction), `ORPHAN.md` (the harvest source).

---

## 1. The read, in one page

You do not have a capability problem. You have a **convergence problem**.

Three lanes are green, and only one of them is mergeable. Two are enormous and one is
51 commits stale. Four more open PRs are 153–553 commits of parallel universe. A
detached orphan holds 4,700 lines of real work that no gate will ever run. The
repository is full of proofs, and a reviewer still cannot answer the only question that
matters: **which tree is true, and what can I reproduce?**

Anyone can add a module. The scarce skill here is deleting a lane without losing its
finding. That is the direction: **narrower lanes, harder seams, one number.**

The one number is **closed-loop coverage** — the fraction of the 93-alert historical
corpus the stack can take end to end:

1. parse unambiguously,
2. price against real bars,
3. execute against a venue ack,
4. replay to a byte-identical book and reconcile clean.

Today it is **0 of 93**. Every segment has a proof; no alert has all four. That is the
honest state of the stack, and it is the only target worth organising around.

---

## 2. The evidence (measured, not remembered)

| PR | Branch | Ahead / behind `main` | Gate | Tests | Verdict |
|---|---|---|---|---|---|
| **#8** | `hardening/correlated-book-guard` | 11 / 0 | green | 708 | **merge first** — linear, current, fail-closed |
| **#1** | `hardening/deterministic-control-plane` | 81 / 0 | green | 824 | **merge second** — current, but docs lie about a closed defect |
| **#3** | `hardening/calibrated-semantic-acceleration` | 7 / **51** | green | 126 | **converge before anything else** |
| #2 | `hardening/precision-intelligence-layer` | 21 / 7 | — | — | **retire** — #3 already contains every commit |
| #4 | `hardening/adversarial-audit-intelligence` | 153 / 7 | — | — | freeze; harvest only |
| #5 | `hardening/performance-tournament-intelligence` | 514 / 7 | — | — | freeze; harvest only |
| #6 | `integration/scanner-context-v1` | 552 / 7 | — | — | freeze; owns scanner context as research only |
| #7 | `apex/program-a-baseline-attestation` | 553 / 7 | — | — | freeze; baseline attestation is a document, not a lane |
| orphan | `feat/risk-budget-context` | 9 vs `main` | not run in CI | — | **no PR** — attached archive, harvest source |

Three diagnoses follow directly:

1. **A queue is not a fleet.** Eight open PRs means eight claims and zero shipped
   truths. The fleet's capacity is not its token budget; it is the reviewer's attention.
   Cap it at three shipped lanes plus one research backlog.
2. **Staleness is a correctness risk, not a tidiness issue.** #3 certifies parsers on a
   tree 51 commits behind the parser it would promote. Every week of drift makes its
   certificate less true. Converge, or stop claiming.
3. **The seams are where money is lost.** 9/8 was a queue between a post and an order.
   The expiry sweep was a state change outside the log. The copied `scorpion_mff` trees
   were two truths for one invariant. Every one of those is a seam, not a feature.

---

## 3. The target: three proofs, one chain

```text
                    PR #3  interpretation truth
                    calibration · novelty · certification
                              |
raw post -> deterministic parse -> decision -> send -> ack/fill -> settle -> reconcile
                    |                  |                              |
              PR #1 correctness     PR #8 execution truth      PR #1 attribution
              event-sourced log     fail-closed ladder         research with fit gates
              fit gates refuse      venue reconciliation       withhold below floor
```

The chain is the architecture. The lanes meet at **artifacts**, never at shared code:

| Seam artifact | Producer | Consumer | Contract |
|---|---|---|---|
| parser version + certificate digest | PR #3 | PR #8 | pinned in the live lane's tests |
| applied-log schema (`signal/fill/expire/halt`) | PR #1 | PR #8 (port) | one definition, no second fingerprint |
| fill-log + venue snapshot schema | PR #8 | PR #1 | the control plane studies the shapes the live lane emits |
| coverage / measurable fraction | PR #1 | fleet | published every time it moves |
| review queue metrics + calibration table | PR #3 | fleet | leading indicator of interpretation risk |

A lane that cannot state its artifact in one line is not a lane; it is a branch.

---

## 4. The five levers

These are the adjectives, made mechanical. Each has an owner and an acceptance command.

### Attractive — a claim a reviewer can reproduce

The PR body becomes a **manifest**, not a story: gate output, test count, corpus
numbers, refusal list, and the one command per claim. `docs/GEMS.md` is already the
model; extend it to every lane. `scripts/fleet/evidence.sh` emits that manifest from
the lane's own tree.

**Built** (`scripts/fleet/evidence.sh`, `scripts/fleet/evidence.py`): every row is a
command and its verdict - identity, lane/branch, drift against `origin/<base>`, purity
(no cross-lane import in either direction; exactly one package in the tree), the gate
per stage, the collected test count, and any `--claim LABEL=COMMAND` the lane wants to
prove. An unrun gate, an unresolvable base, a dirty tree and a two-package tree are
**named refusals** (exit 2) or failures (exit 1), never green rows; `run-lane.sh`
appends the result to the PR body after the commit it describes.

Why it converts: reviewers do not merge prose. They merge a diff they can re-run.

### Powerful — each lane owns an irreducible proof

- PR #8 owns **execution truth** (the only place an order can exist).
- PR #1 owns **correctness truth** (the only place a book must explain itself).
- PR #3 owns **interpretation truth** (the only place a parser may be promoted).

No lane may duplicate another's proof. The moment PR #1 grows a live sender or PR #8
imports the control plane, the stack has two truths and no authority.

### Adaptive — three closed loops with floors

| Loop | Input | Update | Refusal floor |
|---|---|---|---|
| Parser promotion | adjudications | calibration → certificate → pinned parser version | ≥50 labels, no segment regression |
| Pricing / sizing | live fills | TCA → `ask_multiple` → policy | <10 priced fills: refuse to suggest |
| Risk | venue snapshot | reconcile drift → halt buys → recover | malformed snapshot: refuse, never repair |

Adaptive does not mean "self-modifying". It means the system changes only through a
measured loop, and refuses while the evidence is thin.

### Dynamic — event time, not cron time

The decision path is event-driven: Gateway session with resume and ACK, sequence-gap
alarm, quote-age guard, latency budget, expiry as a clock, day roll on the ET trading
date. No polling in the path, no module-level clock, no batch window between a post and
an exit. The market clock is the only clock.

### Intelligent — refusal as a feature

The stack is not smart because it has models. It is smart because it knows what it
cannot prove: the path-signature fit refuses on 107 trades, the tournament says
`INCUMBENT_HELD`, mark-to-market withholds an unpriceable total, TCA withholds 16 of 18
trades, the expiry audit refuses assignment projection, and the semantic certificate
refuses a segment regression. That discipline is the moat. An LLM in the path would
destroy it; an LLM beside the path (review triage, digest, ops) strengthens it.

---

## 5. Where each PR goes

### PR #8 — ship it first, then make it venue-true

**From:** an offline-proven hot path. **To:** the execution authority, with one
credentialed round trip and a scheduled reconciliation.

It merges first because it is linear (0 behind), green (708 tests), fail-closed
(`arm --live` refuses without quote feed and policy), and the only lane whose value
decays with time. Merging it does not place an order; it makes `main` the live path and
resets every other lane's drift.

Then, in order:

1. **Confirmed execution is the authority** — cumulative acks, partial fills, durable
   dedupe, restart replay. Harvest the orphan's `execution.py` by hunk, keep only what
   the live ledger does not already prove.
   **Built** (`execution_book.py`, G17): the append-only `live/execution.jsonl`, the
   cumulative-ack delta, the oversell clamp, the `unknown` (non-terminal) quarantine,
   `sync_engine` as `baseline + confirmed events`, and `book show / replay / verify /
   sync / export`. Proven offline (66 tests); the venue's ack shape is still unconfirmed.
2. **Scheduled venue reconciliation** — build the snapshot adapter against a recorded
   fixture; drift halts buys and pages; exit codes 0/1/2 as specified.
   **Built** (`venue.py`, G18): file and HTTP feeds behind one stamped snapshot, a
   staleness limit, one page per distinct drift, no halt on a dead adapter, and
   `arm --venue-every` wiring it into the tick. The endpoint's field names remain the
   credentialed unknown.
3. **Shadow-live parity, then one real ack** — same code over a recorded session; TCA
   refuses to suggest a multiple under ten priced fills.

Full brief: `pr8.master.md`.

### PR #1 — fix the provenance, then build the data plane

**From:** a second full stack with 824 tests and a doc that says its central defect is
open. **To:** the reference semantics plus the data plane that unblocks every
measurement.

1. **Provenance repair (hours).** `docs/REPLAY_GAP.md` says "open"; commit `afaa7a8`
   closed it (sweep appends `expire`, `verify` replays it, the corpus lockstep test
   pins it). Run `scorpion verify` on the corpus, paste the output, mark the doc closed.
   A stale "open" teaches the next agent to re-fix a closed defect.
2. **Phase 1 data plane** — `bars.py`, `coverage.py`, `sessions.py`. Three analyses
   currently withhold because the join is missing (mark-out 3/107, TCA 2/18,
   attribution 91/93). This is the highest-ROI work in the stack.
3. **Attribution / lifecycle**, then stop. Do not build a second `exposure.py` or
   `stops.py`; the live lane owns those invariants and tests them against money.

Full brief: `pr1.master.md`.

### PR #3 — converge, then become the only door for parser change

**From:** a stacked evidence layer 51 commits behind. **To:** the promotion authority
that PR #8 cannot route around.

1. **Converge and re-cut.** Rebase/merge `main` by intent; re-measure the gate; retire
   **PR #2** (it has zero commits #3 lacks) so there is one semantic PR, not a stale
   stack. A lane that cannot be reviewed in one sitting is not converged.
2. **Publish the contract.** The certificate names the parser pair and its digest; PR #8
   pins the digest and refuses a parser without one. Label-only improvements are
   refused; effect-multiset changes are refused.
3. **Close the human loop.** Backfill the queue, resolve real adjudications, and move a
   named rule `UNCALIBRATED → PROVISIONAL → TRUSTED` only through a certificate. The
   missing input is labels, not another scorer.

Full brief: `pr3.master.md`.

---

## 6. Merge order, and the four gates before any merge

**Order: #8 → #1 → (retire #2) → #3.**

Why #8 first, not last: the money path is the only lane whose risk is *waiting*. It is
current, gated, and cannot place an order without credentials. Merge it, then rebase
the research lanes onto it. The reverse order leaves the live lane accumulating drift
while the fleet perfects paper.

A merge is allowed only when all four hold:

1. **Provenance** — every status line matches a command's output; no doc claims a fix
   that is open, or an open defect that is fixed.
2. **Purity** — no cross-lane import and no copied tree. `scorpion_mff` never imports
   `scorpion`; `src/scorpion` never imports `scorpion_mff`. Both directions are
   grep-able, so gate them in CI.
3. **Drift** — the lane is ≤10 commits behind `main` at review time. More than that is
   converge-only work, not a feature.
4. **Evidence** — the PR body is a manifest: gate output, test count, corpus numbers,
   refusals, and one command per claim.

---

## 7. The next 30 / 60 / 90

**30 days — converge and ship**

- Merge #8. Rebase #1 onto it; close the replay doc; start `bars.py`.
- Rebase #3 onto `main`; retire #2; prove the gate on the converged tree.
- Freeze #4/#5/#6/#7 and the orphan as read-only archives with named owners for harvest.
- Add the fleet audit: drift, attachment, gate, test count, in one table.

**60 days — raise closed-loop coverage from 0**

- #1: bars/coverage/sessions live; measurable corpus ≥50% on at least one channel;
  attribution reproduces the total delta within 0.01R.
- #8: execution book + scheduled reconcile on a recorded snapshot; shadow parity over a
  recorded session.
- #3: first real adjudications; one rule moves a calibration tier through a certificate.

**90 days — one credentialed round trip, one number**

- #8: one real ack with tokens stripped; `tca fills` prices it; `ask_multiple` moves
  only on ≥10 fills; `reconcile` runs on schedule with a clean exit.
- The fleet reports **closed-loop coverage** weekly. The target is not 93/93; it is
  *honest* coverage — every unclosed alert named with the missing input.

---

## 8. The orphan and the archives

`feat/risk-budget-context` is not a PR and will never be merged as a branch. It is
attached to its name (no more detached HEAD) and treated as a **harvest source**:
`evidence.py` → PR #1's measurement lane; `execution.py` gaps → PR #8 move 1;
`context.py` → PR #6 (research only); `risk.py` → duplicate, do not harvest. The full
map and the hunk-level rule are in `ORPHAN.md`.

PRs #4, #5, #6, #7 are frozen archives. They are 153–553 commits wide and answer
questions the stack no longer asks in that shape. Harvest a finding into an owning lane
with that lane's tests; never rebase them, never "revive" them. A branch that cannot be
reviewed is not an asset.

---

## 9. What we will not do

- No dashboards, notebooks, or web UI. The CLI and the JSON manifest are the product.
- No third parser, no second state fingerprint, no second exposure/stops module.
- No LLM in the sub-second path. Agents beside the path, never inside it.
- No conformal sizing on 93 trades; no trend on 12; no model on <20 observations.
- No live feature behind credentials that has not been proven on a recorded payload.
- No new lane until the three shipped lanes are current and the orphan is harvested.

---

## 10. The decisions only the desk owner can make

These are not engineering gaps; they are house calls. They block the stack from
becoming "attractive" because every one of them changes a refusal into a policy:

1. **Robinhood Agentic endpoint** — confirm `MFF_RH_BASE`, the order path, and the field
   names. The adapter does not invent a vendor schema.
2. **Quote feed** — the URL template and the bid/ask JSON paths. Unconfigured, every
   entry fails closed (`no_quote`), which is correct and useless.
3. **Gateway** — bot token with MESSAGE_CONTENT, guild membership for the four channels.
4. **LOTTO inside `options-king`** — excluded like the lotto-* channels, or tradeable?
5. **Exit fallback** — a quote-priced limit with no market fallback can sit unfilled.
   A marketable-limit fallback is a risk decision, not a code decision.
6. **Bare `%` commentary** — a `%` post currently trims (the 9/8 bias, deliberately).
   If commentary becomes a nuisance, require an exit verb or a runner word.

Each answer becomes a lock in `live/RULES.lock.md`, a test, and a line in the manifest.
Until then, the refusal is the feature.

---

## 11. How to know this is working

Three numbers, reported weekly, never combined into a score:

- **Closed-loop coverage** — alerts that parse, price, execute, and replay clean
  (today: 0/93; the target is honest coverage, not a big number).
- **Measurable corpus** — contract-days with bars (today: 3/107, 2.8%).
- **Proven claims** — PR-body claims whose command a second person reproduced
  (today: unpublished; the manifest makes it countable).

If those three move, the stack is becoming attractive, powerful, adaptive, dynamic, and
intelligent for the only reason that matters: it can prove it.

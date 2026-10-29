# The orphan: `feat/risk-budget-context`

**Status:** attached, read-only archive. **No PR. Never merge as a branch.**
**Branch:** `feat/risk-budget-context` (tracks `origin/feat/risk-budget-context`)
**Commit:** `be7b4ed` · **Worktree:** `.worktrees/feat`

Measured 2026-10-29:

```text
vs main          0 behind, 9 ahead
vs PR #8         9 commits here not in #8, 11 commits in #8 not here  (diverged)
footprint        25 files, 4,717 insertions(+), 65 deletions(-)
CI               none. This branch has no lane, no PR, and no gate.
```

The worktree used to sit on a **detached HEAD**. That is why the orphan was invisible:
`git branch` did not list it, `git worktree list` showed `(detached HEAD)`, and nothing
in the fleet would ever run its tests. It is now attached to its branch name. The code is
still an archive; the *location* is now honest.

---

## 1. What it is, and why it exists

This was the live lane's risk/evidence/execution branch before PR #8 grew those
capabilities independently. It was never opened as a pull request, and PR #8 has since
moved 11 commits further (pre-trade budget, add seam, reconcile, Gateway session,
tri-state ledger). The two trees now answer overlapping questions with different code.

It is valuable as a **finding**, not as a merge:

- `evidence.py` is a serious measurement module (BCa bootstrap, Bayesian posterior,
  data-estimated shrinkage, Benjamini-Hochberg, change-point, power).
- `execution.py` is a serious execution-state model (cumulative acks, append-only log,
  baseline persistence, oversell clamp, restart replay).
- `context.py` is a research-only scanner integration with an independently verified
  schema and authority.
- The parser guard it carries catches a real phantom-profit class the live lane still
  admitted (see §3, row 1).

It cannot be merged because `risk.py` duplicates PR #8's, the parser lineage diverged
(live has the strict shorthand anchor; the orphan does not), the stop work is superseded
by `fbdf538`, and the execution design predates the tri-state ledger and reconciliation.
A merge would regress at least three invariants to gain code that is mostly already
better in the live lane.

---

## 2. Harvest map

Take hunks, not files. Each row names the owning lane, what to take, what to leave, and
the acceptance that proves it landed.

| Orphan file | Owner | Take | Leave | Acceptance in the owner |
|---|---|---|---|---|
| `parser.py` (`_DOLLAR_RE` guard + price loop) + 4 tests | **PR #8** | wider option-language lookahead; skip a dollar amount equal to the parsed strike; `finditer` so a later anchored price can win | the rest of the parser diff — the live lane's shorthand anchor and close-phrase rules are newer | **done** in this working tree: `NVDA STRIKE $180 CALLS CLOSED` prices `None`, gate 589 green |
| `evidence.py` (805) | **PR #1** | BCa/bootstrap mean interval, Bayesian `p_up`, data-estimated shrinkage prior, BH q-values, change-point + permutation test, power calculation | module placement and any assumption that a corpus can size a live position | a measurement report that **withholds** below the floor, pinned by a test |
| `execution.py` (650) | **PR #8** | cumulative ack deltas, append-only execution log, baseline persistence, `sync_engine()` from confirmed events, oversell clamp | anything the tri-state ledger + `recover` + `reconcile` already prove better | PR #8 move 1: 5-lot 3-then-2 == one 5-lot; duplicate ack cannot double-fill; restart mid-send leaves UNKNOWN and re-sends nothing |
| `context.py` (269) + fixture | **PR #6** | `scorpion.research-context.v1` verification (schema, authority, identity, checksum), canonicalisation, symbol/session join | any sizing, delay, or block of an MFF order | a broken/malicious snapshot cannot shrink, delay, or block an order; join is attribution-only |
| `engine.py` / `listener.py` / `cli.py` diffs | **PR #8** | a hunk only after `git diff` shows the live lane lacks it (the add seam, exit attribution, day roll, and reconcile are newer live) | wholesale adoption — the live lane has moved past this design | the live lane's own tests; no new test may be invented to justify a hunk |
| `risk.py` (306) | **none** | nothing | the file: it is a near-duplicate of `scorpion_mff/risk.py` (diff is mostly typing/unwrapping) | n/a — do not recreate a second budget authority |
| `tests/test_protective_stop.py` | **none** | nothing | superseded by `fbdf538` (breakeven stop made live) | n/a |
| `live/risk-budget.example.json` | **PR #8** | a sample only if it documents a field the live `live/policy.json` lacks | a second budget schema | `risk validate` accepts it and `arm` still refuses an unreadable budget |

---

## 3. The rule for harvesting

1. **The owner drives.** A hunk is taken in the owning lane, with the owning lane's
   tests. The orphan is never rebased, never merged, never given a lane.
2. **Diff first, always.** The trees diverged 11 commits in one direction and 9 in the
   other. Any hunk taken without a diff is a regression waiting for a market.
3. **One finding, one test.** The orphan's value is the defect it found, not the lines
   it wrote. If the owner cannot write a test that fails without the hunk, the hunk is
   not taken.
4. **Record the disposition.** Update this map in the same commit that harvests or
   rejects a row. An unrecorded decision is how the next agent re-opens an archive.
5. **Then delete.** When every row is `done` or `rejected`, remove the worktree and the
   branch. Keep this document as the record. An archive with no open rows is clutter.

---

## 4. Why the fourth row matters more than it looks

`NVDA STRIKE $180 CALLS CLOSED` parsed in the live lane as a **close at 180.0** before
this harvest. A $1 contract sold at $180 is the same phantom-profit failure the `19%`
bug produced, one wording further out: the regex refused `$240 STRIKE` only when the
word `STRIKE` followed the amount immediately. The orphan had already paid for the
general lesson — a dollar amount followed by *any* option language is a contract term —
and its four tests pin it.

That is the entire argument for keeping an archive instead of deleting it: the code was
mergeable, but the *finding* was the asset. Harvest the finding, delete the branch.

---

## 5. Inspecting it without reviving it

```bash
git -C .worktrees/feat status -sb
git -C .worktrees/feat log --oneline -9
git diff hardening/correlated-book-guard origin/feat/risk-budget-context -- scorpion_mff/
git show origin/feat/risk-budget-context:scorpion_mff/evidence.py | head -60
```

Read-only. Do not commit in the orphan. Do not run the fleet on it. If a hunk is worth
keeping, it moves to an owner and the row above changes — the branch does not move.

---

## 6. The other archives

PRs **#4, #5, #6, #7** are frozen for the same reason at a larger scale (153–553 commits
wide). They are harvest sources with named owners, not candidates. #6 owns the scanner
context row above; #4/#5/#7 may each yield one finding, and that finding goes into a
shipped lane with a test. No rebase, no revival, no agent.

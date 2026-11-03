## Semantic lane — calibrated interpretation and parser certification

This PR is the semantic track: evidence around the deterministic parser, a durable human
review loop, and a certification gate that a parser promotion must pass. It has **no
execution authority** — it cannot mutate the book, approve an effect, size a position, or
submit an order.

**What it is:** parser evidence -> semantic envelope (calibration, novelty, ensemble
uncertainty, contract and quote checks) -> durable review queue with active-learning
priority -> adjudications -> certification (accuracy, actionable precision, ambiguity,
action escalations, ECE, risk coverage, segment parity, semantic state, effect multiset)
-> a digest read back before any promotion relies on it. Shadow models record; they never
decide.

**Convergence:** convergence is a precondition, not a claim. The lane was 51 commits
behind `main` when this brief was written; it is rebased/replayed onto current `main`
and PR #2 is retired (it already contained every #2 commit) before any promotion relies
on this PR. Review the drift number before the diff.

**Gate:** `scripts/gate.sh` — ruff -> mypy `--strict` -> pytest, all green.

**Evidence:**
- `docs/CALIBRATED_SEMANTIC_ACCELERATION.md` (the layer and its refusals)
- `docs/SEMANTIC_CONTROL_PLANE.md` (review queue, certification gates, read-back)
- `docs/PRECISION_INTELLIGENCE.md` (the base lane)

> Fleet-generated. Merged by the fleet, not by hand; review the diff, not this body.

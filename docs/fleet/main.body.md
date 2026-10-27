## Main lane — coded hot path

This PR is the main track: the Discord → Robinhood hot path in `scorpion_mff/`,
with the reduce-only governor, the correlated-book cap, the fill log, the kill
switch, and offline replay over the full alert corpus.

**What it is:** parser → state machine → RH adapter → fill log → kill switch, all
deterministic, standard-library-only, fail-closed. 271 tests green, `ruff` clean,
`mypy --strict` clean.

**Gate:** `scripts/gate.sh` — ruff → mypy `--strict` → pytest, all green.

**Evidence:**
- `HOTPATH.md` (design, non-changes, what is unproven)
- `BACKTEST.md` (the executor against 93 real alerts, and the two defects it found)
- `POLICY.md` (reduce-only risk layer), `TCA.md` (execution quality)

> Fleet-generated. Merged by the fleet, not by hand; review the diff, not this body.

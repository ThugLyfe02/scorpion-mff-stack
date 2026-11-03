## Main lane — coded hot path

This PR is the main track: the Discord → Robinhood hot path in `scorpion_mff/`,
with the reduce-only governor, the correlated-book cap, the fill log, the kill
switch, and offline replay over the full alert corpus.

**What it is:** parser → state machine → RH adapter → fill log → kill switch, all
deterministic, standard-library-only, fail-closed — plus the breakeven stop the trim
armed but nothing read back (`stops.py`, opt-in via `--enforce-breakeven`), a
mark-to-market view whose total withholds itself when the book cannot be priced
(`marks --check`), fill-log integrity verification (`verify`), and the confirmed-execution book
(`execution_book.py`): cumulative acks, partials, a restart replay and an `arm` that
refuses on an unresolved order; and shadow-live parity (`parity.py`): one tape through the
decision path and the live ladder, compared event by event, proven to fail on a partial, a
reject and a transport failure. 787 tests green,
`ruff` clean, `mypy --strict` clean.

**Gate:** `scripts/gate.sh` — ruff → mypy `--strict` → pytest, all green.

**Evidence:**
- `HOTPATH.md` (design, non-changes, what is unproven)
- `BACKTEST.md` (the executor against 93 real alerts, and the two defects it found)
- `POLICY.md` (reduce-only risk layer), `TCA.md` (execution quality)

> Fleet-generated. Merged by the fleet, not by hand; review the diff, not this body.

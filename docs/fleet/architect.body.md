## Architect lane — deterministic control plane

This PR is the architect track: the event-sourced, deterministic control plane in
`src/scorpion/` that replaces the browser/cron path that lost 31 minutes on 9/8.

**What it is:** immutable UTC events -> durable event store -> deterministic parse
(`Entry|Add|Trim|Exit|Ignore|Ambiguous`) -> pure reducer -> paper/review-only
execution. Research frontier (path signatures, Hawkes, GP tails, Wasserstein drift,
rough volatility) shipped only behind gates that refuse under-sized fits.

**Gate:** `scripts/gate.sh` — ruff → mypy `--strict` → pytest, all green.

**Evidence:**
- `docs/ARCHITECTURE_V2.md`, `docs/IMPLEMENTATION_STATUS.md`
- `docs/FRONTIER_7.md` (path signatures and the fit gate that refuses on 107 trades)

> Fleet-generated. Merged by the fleet, not by hand; review the diff, not this body.

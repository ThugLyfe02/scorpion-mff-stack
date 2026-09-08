# Working together without drift

## Source of truth
1. `live/RULES.lock.md` — trading rules (PR to change)
2. `docs/HANDOFF.md` — architecture + failure analysis
3. `mff-backtest/` — historical evidence + sim code
4. `config/` — what the LLM agents currently run (cron prompts) — **not** the target hot path
5. `live/` — fills, misses, health, decisions as they happen

## How to edit
- Open a branch + PR for rule/code changes
- Append to `live/decisions.md` when something operational changes mid-session
- Do not rely on chat alone — chat drifts; this repo does not

## Build priority
1. Discord Gateway listener on 4 channel IDs
2. Deterministic parser → Entry|Exit|Ignore
3. State machine (pipe → size, max2, +25% skip, no chase)
4. RH Agentic adapter + fill logs into `live/fills/`
5. Kill switch file/flag

# Scorpion MFF Stack

Trading-control research stack for **Scorpion Capital**: MoneyForFun Discord options signals,
deterministic signal/replay infrastructure, and a Ross-style gapper research desk.

## Status (2026-09-08)

| Layer | State |
|---|---|
| Discord ingest | **V2 push-based component implemented** in `src/scorpion/ingest/discord.py` |
| Parser / state | **Deterministic V2 implemented** with explicit ambiguity + event-sourced reducer |
| Durable audit | **SQLite WAL event/effect/heartbeat store implemented** |
| Execution | **Paper/shadow + review-only boundary implemented**; no autonomous live submission |
| Backtest / signal corpus | Legacy evidence in `mff-backtest/`; correctness caveats documented |
| Gapper scanner | Agent research only — no scanner binary |

The former browser/LLM cron path is retained only as operational history. It must not be treated as a
sub-second control plane.

## V2 architecture

```text
Discord Gateway MESSAGE_CREATE
  -> immutable UTC RawDiscordMessage
  -> durable event store
  -> deterministic parse Entry|Add|Trim|Exit|Ignore|Ambiguous
  -> conservative association
  -> pure event reducer + generation/idempotency controls
  -> durable proposed effects
  -> paper/shadow OR explicit operator review
```

Research/chat/scanner workloads are outside this path and cannot block Discord ingestion.

## Start here

1. `docs/ARCHITECTURE_V2.md`
2. `docs/IMPLEMENTATION_STATUS.md`
3. `docs/BACKTEST_CORRECTNESS.md`
4. `docs/SECURITY.md`
5. `live/RULES.lock.md`
6. `docs/HANDOFF.md`

## Development

Python 3.13:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Runtime Discord ingest requires an authorized bot token and a strict signal-author allowlist:

```bash
export SCORPION_DISCORD_TOKEN='...'
export SCORPION_SIGNAL_AUTHOR_IDS='1234567890'
export SCORPION_DB='scorpion.db'
scorpion-ingest
```

Do not put tokens, cookies, broker credentials, or account identifiers in Git.

## Historical research

`mff-backtest/` contains the legacy tape/upper-bound research. Do **not** use its headline outputs as
live sizing evidence without the corrections described in `docs/BACKTEST_CORRECTNESS.md`.

## Repository security

This repository contains sensitive operational information and should be private. Masking sensitive
values in a later commit does not purge earlier Git history; see `docs/SECURITY.md`.

## License

Private. Collaborators only.

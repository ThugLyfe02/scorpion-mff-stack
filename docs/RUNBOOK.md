# Paper session runbook (exec-core-v1)

PAPER ONLY. The only order sinks are `PaperBroker` and `ReviewOnlyBroker`; there is no live
order path in this repo. Robinhood stays read-only. Times are ET unless marked.

## Start / stop
- Normal: `scorpion-paper.timer` starts the unit Mon–Fri 09:10 ET. `ExecStartPre` runs
  `python -m scorpion preflight`; on a non-session day (holiday/weekend) it exits clean and the
  session exits 0. The session stops itself at close + 5 min (16:05, or 13:05 on half-days).
- Manual: `sudo systemctl start scorpion-paper` · `sudo systemctl stop scorpion-paper`
  · `journalctl -u scorpion-paper -f`.
- Deploy: only while flat. `git fetch && git checkout <tag>` then `sudo systemctl restart
  scorpion-paper`. No hot reload, no cron, no PID files.

## The NO_TRADE latch (kill switch)
- Status: `python -m scorpion latch status`
- Trip by hand: `python -m scorpion latch trip --reason "<why>"`
- Clear (a person only; restarts never clear it):
  `python -m scorpion latch clear --operator <your-name> --reason "<what you checked>"`
- A fresh install starts NO_TRADE (fail closed). Preflight refuses to start while latched.

## Rotating the Databento key
1. Create the new key in the Databento portal. 2. `sudoedit /etc/scorpion/env` (mode 0600)
and replace `DATABENTO_API_KEY`. 3. `sudo systemctl restart scorpion-paper` while flat.
4. Revoke the old key. Never paste the key into chat, logs, commits or tickets.

## Historical pulls
`python -m scorpion pull --estimate --dataset XNAS.ITCH --schema ohlcv-1m --symbols A,B
--start 2026-10-30 --end 2026-10-31` prints the cost and pulls nothing. Without `--estimate`
the pull runs only if the estimate is ≤ $30; above that it exits 3 unless `--approve-cost` is
passed, which requires Ryan's OK first.

## What each page means
| Page dedup key | Meaning | Do |
|---|---|---|
| `scorpion-paper:feed_silence_ms:<date>` | No message on the whole Databento subscription for > 10 s in session; latched NO_TRADE | Check Databento status / licence / network, then clear the latch |
| `scorpion-paper:clock_skew_ms:<date>` | Event vs receive clock skew > 2 s for 30 s; latched | Check NTP (`timedatectl`), then clear |
| `scorpion-paper:crash:<date>` | A session task crashed; latched; systemd restarts the unit into NO_TRADE | Read `journalctl`, fix, clear |
| (unit failed to start) | Preflight failed: latch, disk < 1 GB, broker mode, rules-lock hash, golden replay or Databento auth | `python -m scorpion preflight` shows which check |

Per-symbol silence (> 60 s on a held/pending symbol) and short clock skew only warn.

## After the session
`python -m scorpion replay --date <D> --input <calls+bars fixture> --out <csv>` reproduces the
fwd-v2.1 log columns for a day. Journal: `$SCORPION_STATE_DIR/journal/<date>.jsonl`; latency
summary: `$SCORPION_STATE_DIR/latency/<date>.json`. None of these files belong in git.

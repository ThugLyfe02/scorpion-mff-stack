#!/usr/bin/env bash
# Regenerate the replay goldens. Needs pandas/pyarrow and the frozen research logger directory
# (FWD_LOGGER_DIR, default /workspace/scorpion_access/fwd_logger). Not run in CI.
set -euo pipefail
FWD_LOGGER_DIR="${FWD_LOGGER_DIR:-/workspace/scorpion_access/fwd_logger}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
python3 - "$tmp" <<'PY'
import json, sys
import pandas as pd
tmp = sys.argv[1]
payload = json.load(open("tests/fixtures/replay_fixture.json"))
rows = [dict(ts=pd.Timestamp(t).tz_convert("UTC"), symbol=s, open=o, high=h, low=l, close=c)
        for s, bars in payload["bars"].items() for t, o, h, l, c in bars]
pd.DataFrame(rows).set_index("ts").to_parquet(f"{tmp}/bars.parquet")
json.dump(payload["calls"], open(f"{tmp}/calls.json", "w"))
PY
python3 "$FWD_LOGGER_DIR/fwd_logger_v2_1.py" --calls "$tmp/calls.json" --bars "$tmp/bars.parquet" \
  --log tests/fixtures/replay_frozen_v21.csv >/dev/null
PYTHONPATH=src python3 -m scorpion replay --input tests/fixtures/replay_fixture.json \
  --out tests/fixtures/replay_golden.csv
echo "goldens written"

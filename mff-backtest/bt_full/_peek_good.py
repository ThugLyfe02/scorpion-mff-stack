import json
from pathlib import Path
# good charts bars
for name in ["2026-08-27_9df07d7c-635b-4fcc-902b-6891959e8a68.json","2026-09-02_f64b55b8-ebf2-4492-a212-f2a5470bd179.json"]:
  p=Path("/workspace/mff-backtest/charts_1m/bars")/name
  if not p.exists():
    print("missing", name); continue
  d=json.load(open(p))
  bars=d.get("bars") or []
  print(name, "n", len(bars))
  if bars:
    print(" first", bars[0])
    print(" last", bars[-1])
# bt_full good
for p in Path("/workspace/mff-backtest/bt_full/bars").glob("*.json"):
  d=json.load(open(p))
  if len(d.get("bars") or [])>10:
    print("good", p.name, len(d["bars"]), d["bars"][0], d["bars"][-1])
    break

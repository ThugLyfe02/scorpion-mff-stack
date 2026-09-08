import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
ET=ZoneInfo("America/New_York")
OUT=Path("/workspace/mff-backtest/bt_full/bars")
src=Path("/workspace/mff-backtest/charts_1m/bars")
def is_rth(ba):
  dt=datetime.fromisoformat(ba.replace("Z","+00:00")).astimezone(ET)
  if dt.weekday()>=5: return False
  t=dt.hour*60+dt.minute
  return 9*60+30 <= t < 16*60
n=0
for p in src.glob("*.json"):
  d=json.load(open(p))
  iid=d.get("instrument_id")
  if not iid: continue
  bars=[]
  for b in d.get("bars") or []:
    # chart bars already filtered non-interpolated sometimes
    if b.get("interpolated"): continue
    ba=b.get("begins_at")
    if not ba or not is_rth(ba): continue
    bars.append({"begins_at":ba,"open":float(b["open"] if "open" in b else b["open_price"]),
                 "high":float(b["high"] if "high" in b else b["high_price"]),
                 "low":float(b["low"] if "low" in b else b["low_price"]),
                 "close":float(b["close"] if "close" in b else b["close_price"])})
  if not bars: continue
  outp=OUT/("%s.json"%iid)
  existing=json.load(open(outp)).get("bars",[]) if outp.exists() else []
  by_t={b["begins_at"]:b for b in existing}
  for b in bars: by_t[b["begins_at"]]=b
  merged=[by_t[k] for k in sorted(by_t)]
  json.dump({"instrument_id":iid,"interval":"5minute","bars":merged}, open(outp,"w"))
  n+=1
print("merged chart bar files", n)

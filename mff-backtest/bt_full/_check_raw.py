import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
ET=ZoneInfo("America/New_York")

# check a few bar files
bd=Path("/workspace/mff-backtest/bt_full/bars")
for p in sorted(bd.glob("*.json"))[:5]:
  d=json.load(open(p))
  print(p.name[:8], "nbars", len(d.get("bars") or []))

# check raw dump for Mar10 - count non-interp RTH
raw=Path("/workspace/agent-tools")
# find dumps with QQQ put
samples=list(raw.glob("*.txt"))[-20:]
for p in samples[:3]:
  text=open(p,encoding="utf-8",errors="ignore").read()
  i=text.find('{"data"')
  if i<0: continue
  try:
    obj=json.loads(text[i:text.rfind("}")+1])
  except: continue
  for r in obj.get("data",{}).get("results") or []:
    bars=r.get("bars") or []
    real=[b for b in bars if not b.get("interpolated")]
    def rth(b):
      dt=datetime.fromisoformat(b["begins_at"].replace("Z","+00:00")).astimezone(ET)
      t=dt.hour*60+dt.minute
      return dt.weekday()<5 and 9*60+30<=t<16*60
    real_rth=[b for b in real if rth(b)]
    print("raw", p.name[:8], r.get("instrument_id","")[:8], "total",len(bars),"real",len(real),"real_rth",len(real_rth))
    if real_rth:
      print("  sample", real_rth[0])
    elif real:
      print("  real non-rth sample", real[0])

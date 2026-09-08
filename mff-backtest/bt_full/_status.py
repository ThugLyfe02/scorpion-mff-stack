import json
from pathlib import Path
trades=json.load(open("/workspace/mff-backtest/bt_full/trades.json"))
cache=json.load(open("/workspace/mff-backtest/charts_1m/cache/instruments.json"))
bars_dir=Path("/workspace/mff-backtest/bt_full/bars")
have=0; empty=0; noid=0
for t in trades:
  key="%s|%s|%.4f|%s"%(t["ticker"],t["side"],float(t["strike"]),t["expiry"])
  iid=cache.get(key)
  if not iid:
    for k,v in cache.items():
      p=k.split("|")
      if p[0]==t["ticker"] and p[1]==t["side"] and p[3]==t["expiry"] and abs(float(p[2])-float(t["strike"]))<1e-6:
        iid=v; break
  if not iid:
    noid+=1; continue
  p=bars_dir/("%s.json"%iid)
  if not p.exists():
    empty+=1; continue
  d=json.load(open(p))
  if d.get("bars"): have+=1
  else: empty+=1
print("trades",len(trades),"have_bars",have,"no_or_empty",empty,"noid",noid)
print("bar files",len(list(bars_dir.glob("*.json"))))
# list ids still needing fetch
need=[]
for f in json.load(open("/workspace/mff-backtest/bt_full/fetch_windows.json")):
  iid=f["instrument_id"]
  p=bars_dir/("%s.json"%iid)
  nb=0
  if p.exists():
    nb=len(json.load(open(p)).get("bars") or [])
  if nb==0:
    need.append(f)
print("windows needing bars",len(need))
json.dump(need, open("/workspace/mff-backtest/bt_full/still_need_bars.json","w"), indent=2)
for f in need[:15]:
  print(f["start"], f["end"], f["instrument_id"], f["keys"][0])

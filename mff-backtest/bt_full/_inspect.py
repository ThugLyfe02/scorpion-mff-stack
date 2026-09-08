import json
from pathlib import Path
bars=list(Path("/workspace/mff-backtest/charts_1m/bars").glob("*.json"))
print("n bar files", len(bars))
b=json.load(open(bars[0]))
print("file", bars[0].name)
print("type", type(b).__name__)
if isinstance(b,dict):
    print("keys", list(b.keys()))
    print("nbars", len(b.get("bars",[])))
    if b.get("bars"): print("bar0", b["bars"][0])
inst=json.load(open("/workspace/mff-backtest/charts_1m/cache/instruments.json"))
print("inst n", len(inst))
trades=json.load(open("/workspace/mff-backtest/bt_full/trades.json"))
keys=set()
for t in trades:
    strike=float(t["strike"])
    k="%s|%s|%.4f|%s" % (t["ticker"], t["side"], strike, t["expiry"])
    keys.add(k)
print("unique", len(keys), "cached", sum(1 for k in keys if k in inst))
missing=[k for k in keys if k not in inst]
print("missing", len(missing))
print("sample missing", missing[:15])
for t in trades:
    if t.get("exit_pct") is None:
        print("noexit", t["date"], t["ticker"], t["outcome"], (t.get("notes") or "")[:90])
# check how many bars have real data
real=0
for p in bars:
    d=json.load(open(p))
    n=len(d.get("bars",[]))
    if n>1: real+=1
print("bar files with >1 bar", real)

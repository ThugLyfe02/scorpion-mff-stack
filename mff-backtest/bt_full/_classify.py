import json
from datetime import date
need=json.load(open("/workspace/mff-backtest/bt_full/need_resolve.json"))
today=date(2026,9,6)
out=[]
for c in need:
  exp=date.fromisoformat(c["expiry"])
  state="expired" if exp<today else "active"
  chain=c["ticker"]
  if chain=="SPX": chain="SPXW"
  out.append({**c, "state": state, "chain_symbol": chain})
  print("%s\t%s\t%s" % (c["key"], state, chain))
json.dump(out, open("/workspace/mff-backtest/bt_full/need_resolve.json","w"), indent=2)
print("TOTAL", len(out))

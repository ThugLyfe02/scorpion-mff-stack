import json
from pathlib import Path

trades = json.load(open("/workspace/mff-backtest/bt_full/trades.json"))
inst = json.load(open("/workspace/mff-backtest/charts_1m/cache/instruments.json"))

def strike_fmt(s):
    return "%.4f" % float(s)

contracts = []
seen = set()
for i, t in enumerate(trades):
    key = "%s|%s|%s|%s" % (t["ticker"], t["side"], strike_fmt(t["strike"]), t["expiry"])
    if key in seen:
        continue
    seen.add(key)
    iid = inst.get(key)
    # also try alternate strike formats already in cache
    if not iid:
        for k,v in inst.items():
            parts = k.split("|")
            if parts[0]==t["ticker"] and parts[1]==t["side"] and parts[3]==t["expiry"] and abs(float(parts[2])-float(t["strike"]))<1e-6:
                iid = v
                break
    contracts.append({
        "key": key,
        "ticker": t["ticker"],
        "side": t["side"],
        "strike": strike_fmt(t["strike"]),
        "expiry": t["expiry"],
        "sample_date": t["date"],
        "instrument_id": iid,
        "trade_indices": [j for j,tt in enumerate(trades) if tt["ticker"]==t["ticker"] and tt["side"]==t["side"] and abs(float(tt["strike"])-float(t["strike"]))<1e-6 and tt["expiry"]==t["expiry"]],
    })

need = [c for c in contracts if not c["instrument_id"]]
print("total unique", len(contracts), "need resolve", len(need), "have id", len(contracts)-len(need))
json.dump(contracts, open("/workspace/mff-backtest/bt_full/contract_map.json","w"), indent=2)
json.dump(need, open("/workspace/mff-backtest/bt_full/need_resolve.json","w"), indent=2)
# print batches for MCP
for c in need:
    print("%s\t%s\t%s\t%s\t%s" % (c["ticker"], c["side"], c["strike"], c["expiry"], c["sample_date"]))

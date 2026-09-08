import json
from pathlib import Path

resolved = json.load(open("/workspace/mff-backtest/bt_full/resolved_ids.json"))
# last batch
resolved.update({
  "QQQ|call|732.0000|2026-08-13": "f63d55ff-3948-4b20-b9cd-016b4023dde9",
  "NVDA|call|227.5000|2026-08-17": "d5d41e2d-9639-4e9d-a901-12f3f64d24b1",
  "WDAY|put|205.0000|2026-08-14": "57f7efd6-e228-4c0d-8eaf-1454aeb88730",
  "TSLA|call|345.0000|2026-08-19": "edb1b208-99a6-495e-8580-8e3547e16d5f",
  "NVDA|call|230.0000|2026-08-28": "5903f60f-3694-4b7c-85f2-d09f46fc3c53",
  "SPX|call|7710.0000|2026-08-18": "e3259b1e-8dfa-49b1-be12-3bf0b7930ab1",
  "QQQ|call|718.0000|2026-08-19": "e63c4c84-e26d-4470-bd36-ec8a694be2d1",
  "QQQ|put|717.0000|2026-08-19": "e8dcc82c-88c0-405c-ac7e-e0163218ca6a",
  "AAPL|call|320.0000|2026-08-21": "c8cf918d-6f93-4c50-ad7e-78558923d845",
  "QQQ|put|710.0000|2026-08-20": "be1015e2-e77c-4c1e-a9be-4ebe1c891c0f",
  "SOXL|put|120.0000|2026-08-21": "933afcda-1f3b-4eee-b264-eff3181daf15",
})
cache = json.load(open("/workspace/mff-backtest/charts_1m/cache/instruments.json"))
cache.update(resolved)
json.dump(cache, open("/workspace/mff-backtest/charts_1m/cache/instruments.json","w"), indent=2)
json.dump(resolved, open("/workspace/mff-backtest/bt_full/resolved_ids.json","w"), indent=2)

trades = json.load(open("/workspace/mff-backtest/bt_full/trades.json"))
# build fetch plan: unique (instrument_id, start_date) covering trade date + next calendar day + maybe to expiry for swings
from datetime import date, timedelta
plan = []
missing_id = []
for i,t in enumerate(trades):
  key = "%s|%s|%.4f|%s" % (t["ticker"], t["side"], float(t["strike"]), t["expiry"])
  iid = cache.get(key)
  if not iid:
    # fuzzy
    for k,v in cache.items():
      parts=k.split("|")
      if parts[0]==t["ticker"] and parts[1]==t["side"] and parts[3]==t["expiry"] and abs(float(parts[2])-float(t["strike"]))<1e-6:
        iid=v; break
  if not iid:
    missing_id.append((i,key)); continue
  d0 = date.fromisoformat(t["date"])
  d1 = d0 + timedelta(days=3)  # cover weekend + next session
  # for swings to expiry, extend
  exp = date.fromisoformat(t["expiry"])
  if exp > d0:
    end = min(exp, d0 + timedelta(days=10))  # cap window
  else:
    end = d0
  end = max(end, d1)
  plan.append({"idx":i, "key":key, "instrument_id":iid, "date":t["date"], "start":d0.isoformat(), "end":end.isoformat(), "expiry":t["expiry"]})

json.dump(plan, open("/workspace/mff-backtest/bt_full/fetch_plan.json","w"), indent=2)
print("trades", len(trades), "plan", len(plan), "missing_id", len(missing_id), "unique_ids", len(set(p["instrument_id"] for p in plan)))
# unique fetch windows by instrument
from collections import defaultdict
by_id = defaultdict(list)
for p in plan:
  by_id[p["instrument_id"]].append(p)
print("instruments to fetch", len(by_id))
# list unique (id, min_start, max_end)
fetches=[]
for iid, items in by_id.items():
  starts=min(x["start"] for x in items)
  ends=max(x["end"] for x in items)
  fetches.append({"instrument_id":iid, "start":starts, "end":ends, "keys":list(set(x["key"] for x in items))})
json.dump(fetches, open("/workspace/mff-backtest/bt_full/fetch_windows.json","w"), indent=2)
print("fetch windows", len(fetches))
for f in fetches[:5]:
  print(f["instrument_id"][:8], f["start"], f["end"], f["keys"][0])

import json
from collections import defaultdict
fetches=json.load(open("/workspace/mff-backtest/bt_full/fetch_windows.json"))
# group by (start,end)
g=defaultdict(list)
for f in fetches:
  g[(f["start"], f["end"])].append(f)
batches=[]
for (s,e), items in sorted(g.items()):
  for i in range(0,len(items),10):
    chunk=items[i:i+10]
    batches.append({
      "start":s, "end":e,
      "instrument_ids":[c["instrument_id"] for c in chunk],
      "keys":[c["keys"][0] for c in chunk],
    })
json.dump(batches, open("/workspace/mff-backtest/bt_full/fetch_batches.json","w"), indent=2)
print("batches", len(batches))
for i,b in enumerate(batches):
  print(i, b["start"], b["end"], len(b["instrument_ids"]), b["keys"][0])

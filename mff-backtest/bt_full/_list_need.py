import json
from pathlib import Path
need=json.load(open("/workspace/mff-backtest/bt_full/still_need_bars.json"))
# prioritize by date desc (recent more likely to have real bars)
need=sorted(need, key=lambda x: x["start"], reverse=True)
print(len(need))
for i,f in enumerate(need):
  print("%d\t%s\t%s\t%s\t%s"%(i,f["start"],f["end"],f["instrument_id"],f["keys"][0]))

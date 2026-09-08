import json, sys
from pathlib import Path
p = Path("/workspace/mff-backtest/bt_full/resolved_ids.json")
ids = json.load(open(p)) if p.exists() else {}
new = json.loads(sys.stdin.read())
ids.update(new)
json.dump(ids, open(p,"w"), indent=2)
need=json.load(open("/workspace/mff-backtest/bt_full/need_resolve.json"))
left=[c for c in need if c["key"] not in ids]
print("saved", len(ids), "left", len(left))
for c in left:
  print("%s|%s|%s|%s|%s|%s" % (c["key"], c["chain_symbol"], c["side"], c["strike"], c["expiry"], c["state"]))

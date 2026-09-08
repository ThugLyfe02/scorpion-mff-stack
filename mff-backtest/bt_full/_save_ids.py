import json
from pathlib import Path
p = Path("/workspace/mff-backtest/bt_full/resolved_ids.json")
ids = json.load(open(p)) if p.exists() else {}
new = {
  "QQQ|put|610.0000|2026-03-10": "00e8ea08-8340-464f-bb4b-f3f96f4fec5b",
  "QQQ|call|600.0000|2026-03-12": "1fea949e-5492-4afe-bd02-ead6a6b02680",
  "QQQ|call|592.0000|2026-03-19": "411b9565-03ad-4a32-88b3-9396e1de4a1d",
  "QQQ|call|585.0000|2026-04-01": "6f832bcf-eeef-4371-b25d-806e710ae49d",
  "QQQ|call|577.0000|2026-04-02": "3606d5ad-b3e0-4afe-9931-ab5b5c104bac",
  "QQQ|put|609.0000|2026-04-13": "7568daa7-d125-4d32-b24f-921131fde949",
  "SPY|call|712.0000|2026-04-21": "3751abbb-620c-43fe-b412-56aea7db72e0",
  "SQQQ|call|43.0000|2026-05-15": "2e3991d5-9bb5-4946-be55-8f116a18fe8b",
  "ORCL|call|190.0000|2026-05-15": "3a92fa2e-a926-4c38-bdf2-6fb371fb53d3",
  "NVDA|call|237.5000|2026-05-15": "5d96f5e0-4580-4e2e-aef5-1e6610a24e14",
}
ids.update(new)
json.dump(ids, open(p,"w"), indent=2)
print("saved", len(ids))
need=json.load(open("/workspace/mff-backtest/bt_full/need_resolve.json"))
left=[c for c in need if c["key"] not in ids]
print("left", len(left))
for c in left[:20]:
  print(c["chain_symbol"], c["side"], c["strike"], c["expiry"], c["state"])

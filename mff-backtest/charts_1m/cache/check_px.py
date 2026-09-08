import json,os
from collections import Counter
d=open("/workspace/agent-tools/317b1828-ba78-4652-a452-b67937ff1758.txt").read()
i=d.find("{"); j=d.rfind("}"); obj=json.loads(d[i:j+1])
for r in obj["data"]["results"]:
    bars=r["bars"]
    closes=[float(b["close_price"]) for b in bars]
    uniq=sorted(set(closes))
    inter=sum(1 for b in bars if b.get("interpolated"))
    print(r["occ_symbol"].strip(), "n", len(bars), "interp", inter, "uniq_closes", len(uniq), "min", min(closes), "max", max(closes), "sample_nonflat", [b for b in bars if float(b["close_price"])!=closes[0]][:3])

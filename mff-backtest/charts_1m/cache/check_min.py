import json
d=open("/workspace/agent-tools/ef0ea705-0233-4f92-ac13-c25e22c6da40.txt").read()
i=d.find("{"); j=d.rfind("}"); obj=json.loads(d[i:j+1])
r=obj["data"]["results"][0]; bars=r["bars"]
closes=[float(b["close_price"]) for b in bars]
real=[b for b in bars if not b.get("interpolated")]
reg=[b for b in bars if b.get("session")=="reg"]
print("n",len(bars),"real",len(real),"reg",len(reg),"uniq",len(set(closes)),"min",min(closes),"max",max(closes))
print("reg sample", reg[:2] if reg else None)
print("keys on bar", bars[200].keys() if len(bars)>200 else bars[0].keys())
print("bar200", bars[200] if len(bars)>200 else None)
